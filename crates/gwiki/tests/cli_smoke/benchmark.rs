use super::*;

#[test]
#[serial_test::serial(serial_db)]
fn benchmark_reports_seeded_fixture_and_degraded_optional_sources() {
    let Some(database_url) = common::postgres_test_database_url() else {
        return;
    };
    let fixture = common::GwikiFixture::new();
    let topic = common::unique_topic("benchmark");
    let _cleanup = common::GwikiScopeCleanup::new(database_url.clone(), "topic", topic.clone());
    let vault = fixture.topic_vault(&topic);
    let wiki_dir = vault.join("knowledge/topics");
    fs::create_dir_all(&wiki_dir).expect("create wiki dir");
    fs::write(
        wiki_dir.join("ownership.md"),
        "# Ownership\n\nOwnership compresses repeated fixture evidence into indexed chunks.\n",
    )
    .expect("write ownership page");
    fs::write(
        wiki_dir.join("rust.md"),
        "# Rust\n\nRust links to [[Ownership]] and cites local source material.\n",
    )
    .expect("write rust page");

    let setup = gwiki_with_database_url(
        &fixture,
        fixture.root(),
        &database_url,
        &["--format", "json", "setup", "--topic", &topic],
    );
    common::assert_success(&setup, "setup");

    let index = gwiki_with_database_url(
        &fixture,
        fixture.root(),
        &database_url,
        &["--format", "json", "index", "--topic", &topic],
    );
    common::assert_success(&index, "index");

    let output = gwiki_with_database_url(
        &fixture,
        fixture.root(),
        &database_url,
        &["--format", "json", "benchmark", "--topic", &topic],
    );
    common::assert_success(&output, "benchmark");
    let payload = common::json_stdout(&output);

    assert_eq!(payload["command"], "benchmark");
    assert_eq!(payload["scope"]["kind"], "topic");
    assert_eq!(payload["scope"]["id"], topic);
    assert!(payload["token_compression"]["available"].as_bool() == Some(true));
    assert!(
        payload["token_compression"]["ratio"]
            .as_f64()
            .is_some_and(|ratio| ratio > 0.0),
        "{payload:#}"
    );
    assert!(
        payload["source_mix"]["available"].as_bool() == Some(true)
            && payload["source_mix"]["total_sources"].as_u64().is_some(),
        "{payload:#}"
    );
    assert_eq!(payload["graph_coverage"]["available"], false);
    assert_eq!(payload["retrieval_precision"]["available"], false);
    assert!(
        payload["degraded_sources"]
            .as_array()
            .is_some_and(|sources| sources.iter().any(|source| source == "falkordb")
                && sources.iter().any(|source| source == "qdrant")
                && sources.iter().any(|source| source == "model_provider")),
        "{payload:#}"
    );
}
