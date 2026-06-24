use super::super::file::write_parsed_file_facts;
use super::super::sink::PostgresCodeFactSink;
use crate::db;
use crate::index::api;
use crate::models::{IndexedProject, ParseResult, Symbol};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires GCODE_POSTGRES_TEST_DATABASE_URL"
)]
#[serial_test::serial(serial_db)]
fn parsed_reindex_preserves_unchanged_symbol_summaries_and_clears_changed_symbols() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_id = unique_test_project_id("gcode-summary-preservation");
    let rel = "src/lib.rs";
    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean summary preservation rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url,
        project_id: project_id.clone(),
    };

    api::upsert_project_stats(
        &mut conn,
        &IndexedProject {
            id: project_id.clone(),
            root_path: "/tmp/gcode-summary-preservation".to_string(),
            total_files: 1,
            total_symbols: 3,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
        },
    )
    .expect("seed project row");

    let unchanged = test_symbol(&project_id, rel, "unchanged", 0, "unchanged-hash");
    let changed = test_symbol(&project_id, rel, "changed", 32, "changed-hash-v1");
    let stale = test_symbol(&project_id, rel, "stale", 64, "stale-hash");
    write_postgres_parsed_file_facts(
        &mut conn,
        &project_id,
        rel,
        "file-hash-v1",
        b"fn unchanged() {}\nfn changed() {}\nfn stale() {}\n",
        vec![unchanged.clone(), changed.clone(), stale.clone()],
    );

    let unchanged_summary = "keep daemon summary";
    let changed_summary = "clear stale daemon summary";
    conn.execute(
        "UPDATE code_symbols SET summary = $1 WHERE id = $2",
        &[&unchanged_summary, &unchanged.id],
    )
    .expect("set unchanged summary");
    conn.execute(
        "UPDATE code_symbols SET summary = $1 WHERE id = $2",
        &[&changed_summary, &changed.id],
    )
    .expect("set changed summary");

    let mut changed_v2 = changed.clone();
    changed_v2.content_hash = "changed-hash-v2".to_string();
    write_postgres_parsed_file_facts(
        &mut conn,
        &project_id,
        rel,
        "file-hash-v2",
        b"// unrelated file edit\nfn unchanged() {}\nfn changed() {}\n",
        vec![unchanged.clone(), changed_v2.clone()],
    );

    assert_eq!(
        symbol_summary(&mut conn, &unchanged.id),
        Some(unchanged_summary.to_string())
    );
    assert_eq!(symbol_summary(&mut conn, &changed.id), None);
    assert_eq!(
        symbol_count(&mut conn, &project_id, rel, &stale.id),
        0,
        "symbols omitted from the latest parse should be deleted"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires GCODE_POSTGRES_TEST_DATABASE_URL"
)]
#[serial_test::serial(serial_db)]
fn postgres_sink_seeds_project_row_before_file_facts() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_id = unique_test_project_id("gcode-project-seed");
    let rel = "src/lib.rs";
    let root_path = Path::new("/tmp/gcode-project-seed");
    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean project seed rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    let seeded_symbol = test_symbol(&project_id, rel, "seeded", 0, "hash-1");
    let seeded_symbol_id = seeded_symbol.id.clone();

    write_postgres_parsed_file_facts_with_root(
        &mut conn,
        &project_id,
        root_path,
        rel,
        "hash-1",
        b"pub fn seeded() {}\n",
        vec![seeded_symbol],
    );

    let root_path_from_db: String = conn
        .query_one(
            "SELECT root_path FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )
        .expect("select seeded project row")
        .get(0);

    assert_eq!(root_path_from_db, root_path.to_string_lossy());
    assert_eq!(
        symbol_count(&mut conn, &project_id, rel, &seeded_symbol_id),
        1
    );
}

fn connect_summary_preservation_test_db() -> (postgres::Client, String) {
    let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL")
        .expect("GCODE_POSTGRES_TEST_DATABASE_URL must be set for summary preservation tests");
    let conn = db::connect_readwrite(&database_url)
        .expect("connect summary preservation PostgreSQL test database");
    (conn, database_url)
}

fn unique_test_project_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    format!("{prefix}-{}-{nanos}", std::process::id())
}

struct SummaryPreservationCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for SummaryPreservationCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
            let _ = cleanup_summary_preservation_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_summary_preservation_project(
    conn: &mut postgres::Client,
    project_id: &str,
) -> anyhow::Result<()> {
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.commit()?;
    Ok(())
}

fn write_postgres_parsed_file_facts(
    conn: &mut postgres::Client,
    project_id: &str,
    rel: &str,
    file_hash: &str,
    source: &[u8],
    symbols: Vec<Symbol>,
) {
    write_postgres_parsed_file_facts_with_root(
        conn,
        project_id,
        Path::new("/tmp/gcode-summary-preservation"),
        rel,
        file_hash,
        source,
        symbols,
    );
}

fn write_postgres_parsed_file_facts_with_root(
    conn: &mut postgres::Client,
    project_id: &str,
    root_path: &Path,
    rel: &str,
    file_hash: &str,
    source: &[u8],
    symbols: Vec<Symbol>,
) {
    let parse_result = ParseResult {
        symbols,
        imports: Vec::new(),
        calls: Vec::new(),
        source: source.to_vec(),
    };
    let mut tx = conn.transaction().expect("start parsed write transaction");
    let mut sink =
        PostgresCodeFactSink::new(&mut tx, project_id, root_path).expect("seed project row");
    write_parsed_file_facts(
        &mut sink,
        project_id,
        rel,
        "rust",
        file_hash,
        source.len(),
        &parse_result,
    )
    .expect("write parsed file facts to PostgreSQL");
    tx.commit().expect("commit parsed write transaction");
}

fn test_symbol(
    project_id: &str,
    rel: &str,
    name: &str,
    byte_start: usize,
    content_hash: &str,
) -> Symbol {
    Symbol {
        id: Symbol::make_id(project_id, rel, name, "function", byte_start),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        name: name.to_string(),
        qualified_name: name.to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start,
        byte_end: byte_start + name.len(),
        line_start: 1,
        line_end: 1,
        signature: Some(format!("fn {name}()")),
        docstring: None,
        parent_symbol_id: None,
        content_hash: content_hash.to_string(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn symbol_summary(conn: &mut postgres::Client, symbol_id: &str) -> Option<String> {
    conn.query_one(
        "SELECT summary FROM code_symbols WHERE id = $1",
        &[&symbol_id],
    )
    .expect("query symbol summary")
    .try_get(0)
    .expect("decode symbol summary")
}

fn symbol_count(conn: &mut postgres::Client, project_id: &str, rel: &str, symbol_id: &str) -> i64 {
    conn.query_one(
        "SELECT COUNT(*)::BIGINT
         FROM code_symbols
         WHERE project_id = $1 AND file_path = $2 AND id = $3",
        &[&project_id, &rel, &symbol_id],
    )
    .expect("query symbol count")
    .try_get(0)
    .expect("decode symbol count")
}
