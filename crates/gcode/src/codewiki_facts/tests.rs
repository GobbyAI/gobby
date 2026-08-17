use std::path::Path;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use serial_test::serial;
use uuid::Uuid;

use super::*;
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::graph::code_graph::GraphReadError;
use crate::index::api::{self, IndexOptions, IndexRequest};

#[test]
fn graph_query_reports_available_non_empty() -> Result<()> {
    let outcome = graph::classify_query(Ok(vec!["edge"]), 2)?;
    assert_eq!(outcome, GraphOutcome::Available(vec!["edge"]));
    Ok(())
}

#[test]
fn graph_query_reports_unconfigured_with_reason() -> Result<()> {
    let outcome = graph::classify_query::<String>(
        Err(anyhow::Error::new(GraphReadError::NotConfigured)),
        10,
    )?;
    assert!(matches!(
        outcome,
        GraphOutcome::Unavailable { reason } if reason.contains("not configured")
    ));
    Ok(())
}

#[test]
fn graph_query_reports_unreachable_with_reason() -> Result<()> {
    let outcome = graph::classify_query::<String>(
        Err(anyhow::Error::new(GraphReadError::Unreachable {
            message: "connection refused".to_string(),
        })),
        10,
    )?;
    assert!(matches!(
        outcome,
        GraphOutcome::Unavailable { reason } if reason.contains("connection refused")
    ));
    Ok(())
}

#[test]
fn graph_query_preserves_genuine_errors() {
    let error = graph::classify_query::<String>(
        Err(anyhow::Error::new(GraphReadError::QueryFailed {
            message: "invalid query".to_string(),
        })),
        10,
    )
    .expect_err("query failures must remain errors");
    assert!(error.to_string().contains("invalid query"));
}

#[test]
fn graph_query_marks_exact_limit_as_truncated() -> Result<()> {
    let outcome = graph::classify_query(Ok(vec!["one", "two"]), 2)?;
    assert_eq!(outcome, GraphOutcome::Truncated(vec!["one", "two"]));
    Ok(())
}

#[test]
fn scoped_edge_overfetch_marks_only_additional_rows_as_truncated() -> Result<()> {
    let complete = graph::classify_overfetch(Ok(vec!["one", "two"]), 2)?;
    assert_eq!(complete, GraphOutcome::Available(vec!["one", "two"]));
    let truncated = graph::classify_overfetch(Ok(vec!["one", "two", "three"]), 2)?;
    assert_eq!(truncated, GraphOutcome::Truncated(vec!["one", "two"]));
    Ok(())
}

#[test]
fn graph_query_reports_successful_empty() -> Result<()> {
    let outcome = graph::classify_query::<String>(Ok(Vec::new()), 10)?;
    assert_eq!(outcome, GraphOutcome::Empty);
    Ok(())
}

#[test]
fn disabled_freshness_admission_is_quiet_and_does_not_resolve_context() -> Result<()> {
    let status = ensure_project_fresh(Path::new("/missing/codewiki-facts-project"), true)?;
    assert_eq!(status, FreshnessStatus::Checked);
    Ok(())
}

#[test]
#[serial(serial_db)]
fn facade_reads_owned_facts_from_a_temp_indexed_project() -> Result<()> {
    let fixture = IndexedFixture::new()?;
    let all = ScopeSelector::all();
    let files = fixture.facts.scoped_files(&all)?;
    let source = files
        .iter()
        .find(|file| file.path == "src/lib.rs")
        .expect("indexed source file is visible");

    let symbols = fixture.facts.symbols_in(std::slice::from_ref(&source.id))?;
    let symbol = symbols
        .iter()
        .find(|symbol| symbol.name == "fixture_add")
        .expect("fixture function is indexed");
    assert_eq!(symbol.language, "rust");
    assert!(!symbol.content_hash.is_empty());

    let outline = fixture.facts.symbols_for_file(&source.id)?;
    assert!(outline.iter().any(|item| item.id == symbol.id));
    assert_eq!(
        fixture
            .facts
            .symbol_by_id(&symbol.id)?
            .expect("symbol lookup succeeds")
            .qualified_name,
        symbol.qualified_name
    );

    let chunks = fixture
        .facts
        .leading_chunks(std::slice::from_ref(&source.id))?;
    assert!(
        chunks
            .first()
            .expect("indexed file has a leading chunk")
            .content
            .contains("fixture_add")
    );

    let search = fixture.facts.search("fixture_add", 10)?;
    assert!(search.iter().any(|hit| hit.symbol.id == symbol.id));

    let grep = fixture.facts.grep("fixture_add", &all)?;
    assert!(grep.hits.iter().any(|hit| hit.path == "src/lib.rs"));
    Ok(())
}

#[test]
#[serial(serial_db)]
fn facade_handle_is_clone_send_sync_and_reuses_read_connection() -> Result<()> {
    fn assert_clone_send_sync<T: Clone + Send + Sync>() {}

    assert_clone_send_sync::<CodewikiFacts>();
    let fixture = IndexedFixture::new()?;
    let clone = fixture.facts.clone();
    assert!(Arc::ptr_eq(&fixture.facts.context, &clone.context));
    assert!(Arc::ptr_eq(
        &fixture.facts.read_connection,
        &clone.read_connection
    ));

    let first_pid: i32 = {
        let mut conn = fixture.facts.read_connection()?;
        let read_only: String = conn.query_one("SHOW transaction_read_only", &[])?.get(0);
        assert_eq!(read_only, "on");
        conn.query_one("SELECT pg_backend_pid()", &[])?.get(0)
    };
    let second_pid: i32 = clone
        .read_connection()?
        .query_one("SELECT pg_backend_pid()", &[])?
        .get(0);
    assert_eq!(first_pid, second_pid);
    Ok(())
}

struct IndexedFixture {
    _root: tempfile::TempDir,
    facts: CodewikiFacts,
    database_url: String,
    project_id: String,
}

impl IndexedFixture {
    fn new() -> Result<Self> {
        let root = tempfile::tempdir()?;
        std::fs::create_dir_all(root.path().join("src"))?;
        std::fs::write(
            root.path().join("Cargo.toml"),
            "[package]\nname = \"codewiki-facts-fixture\"\nversion = \"0.1.0\"\nedition = \"2024\"\n",
        )?;
        std::fs::write(
            root.path().join("src/lib.rs"),
            "pub fn fixture_add(left: i32, right: i32) -> i32 { left + right }\n",
        )?;

        let nonce = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let project_id = Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("codewiki-facts-{nonce}").as_bytes(),
        )
        .to_string();
        let database_url =
            crate::test_env::postgres_test_database_url("codewiki facts facade tests");
        let ctx = Context {
            database_url: database_url.clone(),
            project_root: root.path().to_path_buf(),
            project_id: project_id.clone(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        };
        api::index_files(
            IndexRequest {
                project_root: root.path().to_path_buf(),
                path_filter: None,
                explicit_files: Vec::new(),
                full: true,
                require_cpp_semantics: false,
                sync_projections: false,
            },
            &ctx,
            IndexOptions::default(),
        )?;

        Ok(Self {
            _root: root,
            facts: CodewikiFacts::from_context(ctx),
            database_url,
            project_id,
        })
    }
}

impl Drop for IndexedFixture {
    fn drop(&mut self) {
        let Ok(mut conn) = crate::db::connect_readwrite(&self.database_url) else {
            return;
        };
        let Ok(project_id) = crate::db::id_param(&self.project_id) else {
            return;
        };
        let _ = conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_calls WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_imports WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_symbols WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            &[&project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        );
    }
}
