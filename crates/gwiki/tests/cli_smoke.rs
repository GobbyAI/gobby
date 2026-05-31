use std::fs;
use std::path::Path;
use std::process::{Command, Output};

use gobby_wiki::session::{AcceptedResearchNote, DaemonDispatch, ResearchScope, ResearchSession};
use gobby_wiki::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest,
};

fn gwiki(hub: &Path, cwd: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
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
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
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
        &["--format", "json", "setup", "--topic", &topic],
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

fn cleanup_postgres_topic(database_url: &str, topic: &str) -> Result<(), postgres::Error> {
    let mut client = postgres::Client::connect(database_url, postgres::NoTls)?;
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
            "--format",
            "json",
            "--topic",
            "rust",
            "research",
            "--resume",
            "How should ownership evidence compile?",
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
            "--format",
            "json",
            "--topic",
            "rust",
            "research",
            "--resume",
            "ignored on resume",
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
