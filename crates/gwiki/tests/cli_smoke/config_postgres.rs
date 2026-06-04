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
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("pg-writer-contract");
    let invalid_database_url = "postgresql://127.0.0.1:1/gwiki";

    fs::create_dir_all(topic.vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        topic.vault.join("wiki/topics/durable-search.md"),
        "# Durable Search\n\nConfigured indexing must use PostgreSQL.\n",
    )
    .expect("write topic page");

    let index = gwiki_with_database_url(
        &fixture,
        fixture.root(),
        invalid_database_url,
        &["--format", "json", "index", "--topic", &topic.name],
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

mod serial_db {
    use super::*;

    #[test]
    #[serial_test::serial(serial_db)]
    fn configured_postgres_index_feeds_configured_search_when_test_database_is_available() {
        let Some(database_url) = common::postgres_test_database_url() else {
            eprintln!(
                "skipping configured_postgres_index_feeds_configured_search_when_test_database_is_available: GWIKI_POSTGRES_TEST_DATABASE_URL/GCODE_POSTGRES_TEST_DATABASE_URL is not set"
            );
            return;
        };

        let fixture = common::GwikiFixture::new();
        let topic = fixture.init_topic("pg-index-search");
        let _cleanup =
            common::GwikiScopeCleanup::new(database_url.clone(), "topic", topic.name.clone());

        let setup = gwiki_with_database_url(
            &fixture,
            fixture.root(),
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
                &topic.name,
            ],
        );
        common::assert_success(&setup, "setup");

        fs::create_dir_all(topic.vault.join("wiki/topics")).expect("create topic dir");
        fs::write(
            topic.vault.join("wiki/topics/durable-search.md"),
            "# Durable Search\n\nDurable bm25needle content is searchable after indexing.\n",
        )
        .expect("write topic page");

        let index = gwiki_with_database_url(
            &fixture,
            fixture.root(),
            &database_url,
            &["--format", "json", "index", "--topic", &topic.name],
        );
        common::assert_success(&index, "index");

        let search = gwiki_with_database_url(
            &fixture,
            fixture.root(),
            &database_url,
            &[
                "--format",
                "json",
                "search",
                "--topic",
                &topic.name,
                "bm25needle",
                "--limit",
                "3",
            ],
        );
        common::assert_success(&search, "search");
        let search_payload = common::json_stdout(&search);
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
}
