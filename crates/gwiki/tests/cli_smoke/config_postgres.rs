use super::*;

#[test]
fn command_outputs_do_not_emit_static_placeholder_results() {
    let fixture = common::GwikiFixture::new();
    let topic = common::unique_topic("placeholder-output");
    let source = fixture.root().join("placeholder-source.md");
    fs::write(
        &source,
        "# Placeholder Source\n\nPlaceholder fixture content for ingest.\n",
    )
    .expect("write source");

    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--topic", &topic],
    );
    common::assert_success(&init, "init");

    let vault = fixture.topic_vault(&topic);
    fs::create_dir_all(vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        vault.join("wiki/topics/ownership.md"),
        "# Ownership\n\nOwnership placeholderneedle evidence.\n",
    )
    .expect("write ownership page");
    fs::write(
        vault.join("wiki/topics/rust.md"),
        "# Rust\n\nRust links to [[Ownership]]. Missing [[Borrow checker]].\n",
    )
    .expect("write rust page");

    let setup = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "setup", "--topic", &topic],
    );
    common::assert_success(&setup, "setup");

    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            &topic,
            source.to_str().expect("source path utf8"),
        ],
    );
    common::assert_success(&ingest, "ingest-file");

    let index = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "index", "--topic", &topic],
    );
    common::assert_success(&index, "index");

    let search = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "search",
            "--topic",
            &topic,
            "placeholderneedle",
        ],
    );
    common::assert_success(&search, "search");

    let backlinks = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "backlinks",
            "--topic",
            &topic,
            "wiki/topics/ownership.md",
        ],
    );
    common::assert_success(&backlinks, "backlinks");

    let suggestions = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "link-suggest",
            "--topic",
            &topic,
            "--limit",
            "3",
        ],
    );
    common::assert_success(&suggestions, "link-suggest");

    let text_topic = common::unique_topic("placeholder-text");
    let text_init = gwiki(&fixture, fixture.root(), &["init", "--topic", &text_topic]);
    common::assert_success(&text_init, "text init");
    let text_source = fixture.root().join("placeholder-source-text.md");
    fs::write(
        &text_source,
        "# Placeholder Text Source\n\nDifferent content for text ingest.\n",
    )
    .expect("write text source");
    let text_setup = gwiki(&fixture, fixture.root(), &["setup", "--topic", &topic]);
    common::assert_success(&text_setup, "text setup");
    let text_index = gwiki(&fixture, fixture.root(), &["index", "--topic", &topic]);
    common::assert_success(&text_index, "text index");
    let text_ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "ingest-file",
            "--topic",
            &topic,
            text_source.to_str().expect("text source path utf8"),
        ],
    );
    common::assert_success(&text_ingest, "text ingest-file");

    let init_json = common::json_stdout(&init);
    let checks = [
        (
            "init",
            serde_json::to_string_pretty(&init_json).expect("pretty init JSON"),
            vec!["\"created\": []"],
        ),
        ("setup", pretty_stdout(&setup), vec!["\"objects\": []"]),
        ("ingest", pretty_stdout(&ingest), vec!["\"created\": []"]),
        (
            "index",
            pretty_stdout(&index),
            vec!["\"documents\": 0", "\"chunks\": 0", "\"links\": 0"],
        ),
        ("search", pretty_stdout(&search), vec!["\"results\": []"]),
        (
            "backlinks",
            pretty_stdout(&backlinks),
            vec!["\"backlinks\": []"],
        ),
        (
            "suggestions",
            pretty_stdout(&suggestions),
            vec!["\"suggestions\": []"],
        ),
        (
            "text init",
            String::from_utf8_lossy(&text_init.stdout).into_owned(),
            vec!["Init ready"],
        ),
        (
            "text setup",
            String::from_utf8_lossy(&text_setup.stdout).into_owned(),
            vec!["Setup ready"],
        ),
        (
            "text index",
            String::from_utf8_lossy(&text_index.stdout).into_owned(),
            vec!["Index ready"],
        ),
        (
            "text ingest",
            String::from_utf8_lossy(&text_ingest.stdout).into_owned(),
            vec!["Ingest file ready"],
        ),
    ];

    for (label, output, patterns) in checks {
        for pattern in patterns {
            assert!(
                !output.contains(pattern),
                "{label} output still contains placeholder pattern {pattern:?}:\n{output}"
            );
        }
    }
}

fn pretty_stdout(output: &std::process::Output) -> String {
    serde_json::to_string_pretty(&common::json_stdout(output)).expect("pretty JSON stdout")
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
        let results = search_payload["results"]
            .as_array()
            .expect("search results array");
        assert!(
            results.iter().any(|result| {
                result["wiki_page"] == "wiki/topics/durable-search.md"
                    && result["sources"]
                        .as_array()
                        .is_some_and(|sources| sources.iter().any(|source| source == "bm25"))
            }),
            "{search_payload:#}"
        );
    }
}
