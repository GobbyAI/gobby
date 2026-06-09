mod common;

#[test]
fn search_json_includes_scope() {
    let fixture = common::GwikiFixture::new();

    let output = fixture.output(&["--format", "json", "search", "--topic", "rust", "ownership"]);
    common::assert_success(&output, "search");

    let payload = common::json_stdout(&output);
    assert_eq!(payload["command"], "search");
    assert_eq!(payload["query"], "ownership");
    assert_eq!(payload["scope"]["kind"], "topic");
    assert_eq!(payload["scope"]["id"], "rust");
    assert!(payload["results"].is_array());
}

mod serial_db {
    use super::common;

    #[test]
    #[serial_test::serial(serial_db)]
    fn search_uses_configured_postgres_bm25_backend() {
        let fixture = common::GwikiFixture::new();

        let output = fixture.output_with_database_url_in(
            fixture.root(),
            "not-a-postgres-url",
            &["--format", "json", "search", "--topic", "rust", "ownership"],
        );

        assert!(
            !output.status.success(),
            "search unexpectedly ignored configured PostgreSQL\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert_json_error(
            &output,
            "config_error",
            "failed to connect to PostgreSQL for gwiki search",
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn index_uses_configured_postgres_store() {
        let fixture = common::GwikiFixture::new();
        let wiki_page = fixture.topic_vault("rust").join("knowledge/topics/rust.md");
        std::fs::create_dir_all(wiki_page.parent().expect("wiki page parent")).expect("mkdir wiki");
        std::fs::write(&wiki_page, "# Ownership\n\nBorrowing and lifetimes.\n")
            .expect("write wiki");

        let output = fixture.output_with_database_url_in(
            fixture.root(),
            "not-a-postgres-url",
            &["--format", "json", "index", "--topic", "rust"],
        );

        assert!(
            !output.status.success(),
            "index unexpectedly ignored configured PostgreSQL\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert_json_error(
            &output,
            "config_error",
            "failed to connect to PostgreSQL for gwiki index",
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn ingest_uses_configured_postgres_store() {
        let fixture = common::GwikiFixture::new();
        let source = fixture.root().join("source.md");
        std::fs::write(&source, "# Ownership\n\nBorrowing and lifetimes.\n").expect("write source");

        let output = fixture.output_with_database_url_in(
            fixture.root(),
            "not-a-postgres-url",
            &[
                "--format",
                "json",
                "ingest-file",
                source.to_str().expect("source path is UTF-8"),
                "--topic",
                "rust",
            ],
        );

        assert!(
            !output.status.success(),
            "ingest unexpectedly ignored configured PostgreSQL\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert_json_error(
            &output,
            "config_error",
            "failed to connect to PostgreSQL for gwiki ingest-file",
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn configured_postgres_index_feeds_configured_search() {
        let Some(database_url) = common::postgres_test_database_url() else {
            return;
        };
        let fixture = common::GwikiFixture::new();
        let topic = common::unique_topic("rust");
        let _cleanup = common::GwikiScopeCleanup::new(database_url.clone(), "topic", topic.clone());
        let unique_term = common::unique_topic("durablebm25").replace('-', "");
        let wiki_page = fixture
            .topic_vault(&topic)
            .join("knowledge/topics/ownership.md");
        std::fs::create_dir_all(wiki_page.parent().expect("wiki page parent")).expect("mkdir wiki");
        std::fs::write(
            &wiki_page,
            format!("# Ownership\n\nThe {unique_term} token should be durable.\n"),
        )
        .expect("write wiki");

        let setup = fixture.output_with_database_url_in(
            fixture.root(),
            &database_url,
            &["--format", "json", "setup", "--topic", &topic],
        );
        common::assert_success(&setup, "setup");

        let index = fixture.output_with_database_url_in(
            fixture.root(),
            &database_url,
            &["--format", "json", "index", "--topic", &topic],
        );
        common::assert_success(&index, "index");

        let search = fixture.output_with_database_url_in(
            fixture.root(),
            &database_url,
            &[
                "--format",
                "json",
                "search",
                "--topic",
                &topic,
                &unique_term,
            ],
        );
        common::assert_success(&search, "search");

        let payload: serde_json::Value =
            serde_json::from_slice(&search.stdout).expect("search JSON");
        let results = payload["results"].as_array().expect("results array");
        assert!(
            results
                .iter()
                .any(|result| result["wiki_page"].as_str() == Some("knowledge/topics/ownership.md")),
            "search did not return indexed wiki page\nstdout:\n{}",
            String::from_utf8_lossy(&search.stdout)
        );
    }

    fn assert_json_error(output: &std::process::Output, code: &str, message_contains: &str) {
        let payload = common::json_stderr(output);
        assert_eq!(payload["code"].as_str(), Some(code));
        assert!(
            payload["message"]
                .as_str()
                .is_some_and(|message| message.contains(message_contains)),
            "stderr:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
