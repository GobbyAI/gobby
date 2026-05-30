use std::process::Command;

#[test]
fn search_json_includes_scope() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");

    let output = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["--format", "json", "search", "--topic", "rust", "ownership"])
        .env("GOBBY_WIKI_HUB", &hub)
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "search failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let payload: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout is JSON");
    assert_eq!(payload["command"], "search");
    assert_eq!(payload["query"], "ownership");
    assert_eq!(payload["scope"]["kind"], "topic");
    assert_eq!(payload["scope"]["id"], "rust");
    assert!(payload["results"].is_array());
}

#[test]
fn search_uses_configured_postgres_bm25_backend() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");

    let output = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["--format", "json", "search", "--topic", "rust", "ownership"])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", "not-a-postgres-url")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki binary runs");

    assert!(
        !output.status.success(),
        "search unexpectedly ignored configured PostgreSQL\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("failed to connect to PostgreSQL for gwiki search"),
        "stderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
