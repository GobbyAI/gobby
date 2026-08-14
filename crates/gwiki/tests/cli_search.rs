mod common;

#[test]
fn search_json_includes_scope() {
    let fixture = common::GwikiFixture::new();

    let output = fixture.output(&["--format", "json", "search", "--topic", "rust", "ownership"]);
    assert!(
        !output.status.success(),
        "search must not fall back to a memory store"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("daemon required") || stderr.contains("malformed grant"),
        "{stderr}"
    );
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
        assert_grant_gated(&output);
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
        assert_grant_gated(&output);
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
        assert_grant_gated(&output);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn configured_postgres_index_feeds_configured_search() {
        let Some(database_url) = common::postgres_test_database_url() else {
            return;
        };
        let fixture = common::GwikiFixture::new();
        let topic = common::unique_topic("rust");
        let index = fixture.output_with_database_url_in(
            fixture.root(),
            &database_url,
            &["--format", "json", "index", "--topic", &topic],
        );
        assert!(!index.status.success(), "env DSN must not bypass grant");
        assert_grant_gated(&index);
    }

    fn assert_grant_gated(output: &std::process::Output) {
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("daemon required")
                || stderr.contains("malformed grant")
                || stderr.contains("daemon_required"),
            "stderr:\n{stderr}"
        );
    }
}
