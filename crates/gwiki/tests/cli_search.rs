use std::time::{SystemTime, UNIX_EPOCH};

mod common;

const GWIKI_SCOPE_TABLES: &[&str] = &[
    "gwiki_ingestions",
    "gwiki_links",
    "gwiki_chunks",
    "gwiki_sources",
    "gwiki_documents",
];

#[test]
fn search_json_includes_scope() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");

    let output = common::gwiki_command()
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

    let output = common::gwiki_command()
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
    assert_json_error(
        &output,
        "config_error",
        "failed to connect to PostgreSQL for gwiki search",
    );
}

#[test]
fn index_uses_configured_postgres_store() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let wiki_page = hub.join("topics").join("rust").join("wiki/topics/rust.md");
    std::fs::create_dir_all(wiki_page.parent().expect("wiki page parent")).expect("mkdir wiki");
    std::fs::write(&wiki_page, "# Ownership\n\nBorrowing and lifetimes.\n").expect("write wiki");

    let output = common::gwiki_command()
        .args(["--format", "json", "index", "--topic", "rust"])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", "not-a-postgres-url")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki binary runs");

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
fn ingest_uses_configured_postgres_store() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let source = tmp.path().join("source.md");
    std::fs::write(&source, "# Ownership\n\nBorrowing and lifetimes.\n").expect("write source");

    let output = common::gwiki_command()
        .args([
            "--format",
            "json",
            "ingest-file",
            source.to_str().expect("source path is UTF-8"),
            "--topic",
            "rust",
        ])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", "not-a-postgres-url")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki binary runs");

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
fn configured_postgres_index_feeds_configured_search() {
    let Some(database_url) = postgres_test_database_url() else {
        return;
    };
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let suffix = unique_suffix();
    let topic = format!("rust-{suffix}");
    let _cleanup = GwikiScopeCleanup::new(database_url.clone(), "topic", topic.clone());
    let unique_term = format!("durablebm25{suffix}");
    let wiki_page = hub
        .join("topics")
        .join(&topic)
        .join("wiki/topics/ownership.md");
    std::fs::create_dir_all(wiki_page.parent().expect("wiki page parent")).expect("mkdir wiki");
    std::fs::write(
        &wiki_page,
        format!("# Ownership\n\nThe {unique_term} token should be durable.\n"),
    )
    .expect("write wiki");

    let setup = common::gwiki_command()
        .args(["--format", "json", "setup", "--topic", &topic])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", &database_url)
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki setup runs");
    assert_command_success("setup", &setup);

    let index = common::gwiki_command()
        .args(["--format", "json", "index", "--topic", &topic])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", &database_url)
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki index runs");
    assert_command_success("index", &index);

    let search = common::gwiki_command()
        .args([
            "--format",
            "json",
            "search",
            "--topic",
            &topic,
            &unique_term,
        ])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GWIKI_DATABASE_URL", &database_url)
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .output()
        .expect("gwiki search runs");
    assert_command_success("search", &search);

    let payload: serde_json::Value = serde_json::from_slice(&search.stdout).expect("search JSON");
    let results = payload["results"].as_array().expect("results array");
    assert!(
        results
            .iter()
            .any(|result| result["wiki_page"].as_str() == Some("wiki/topics/ownership.md")),
        "search did not return indexed wiki page\nstdout:\n{}",
        String::from_utf8_lossy(&search.stdout)
    );
}

fn postgres_test_database_url() -> Option<String> {
    std::env::var("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .ok()
        .or_else(|| std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok())
        .filter(|value| !value.trim().is_empty())
}

fn unique_suffix() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    format!(
        "{}-{nanos}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    )
}

fn assert_command_success(command: &str, output: &std::process::Output) {
    assert!(
        output.status.success(),
        "{command} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn assert_json_error(output: &std::process::Output, code: &str, message_contains: &str) {
    let payload: serde_json::Value =
        serde_json::from_slice(&output.stderr).expect("stderr is JSON error");
    assert_eq!(payload["code"].as_str(), Some(code));
    assert!(
        payload["message"]
            .as_str()
            .is_some_and(|message| message.contains(message_contains)),
        "stderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn cleanup_gwiki_scope(database_url: &str, scope_kind: &str, scope_id: &str) {
    let Ok(mut client) = gobby_core::postgres::connect_readwrite(database_url) else {
        return;
    };
    for table in GWIKI_SCOPE_TABLES {
        // Safe interpolation: `table` comes only from `GWIKI_SCOPE_TABLES`,
        // a closed whitelist of gwiki-owned table names above.
        assert_gwiki_scope_table(table);
        let sql = format!("DELETE FROM {table} WHERE scope_kind = $1 AND scope_id = $2");
        let _ = client.execute(&sql, &[&scope_kind, &scope_id]);
    }
}

fn assert_gwiki_scope_table(table: &str) {
    assert!(
        GWIKI_SCOPE_TABLES.contains(&table)
            && table.starts_with("gwiki_")
            && table.chars().all(|ch| ch.is_ascii_lowercase() || ch == '_'),
        "cleanup table must be a whitelisted gwiki table: {table}"
    );
}

struct GwikiScopeCleanup {
    database_url: String,
    scope_kind: &'static str,
    scope_id: String,
}

impl GwikiScopeCleanup {
    fn new(database_url: String, scope_kind: &'static str, scope_id: String) -> Self {
        Self {
            database_url,
            scope_kind,
            scope_id,
        }
    }
}

impl Drop for GwikiScopeCleanup {
    fn drop(&mut self) {
        cleanup_gwiki_scope(&self.database_url, self.scope_kind, &self.scope_id);
    }
}
