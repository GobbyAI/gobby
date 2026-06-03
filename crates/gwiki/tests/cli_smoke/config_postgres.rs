use super::*;

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
