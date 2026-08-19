use super::super::file::write_parsed_file_facts;
use super::super::sink::CodeFactSink;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, InheritanceRelation,
    ParseResult, Symbol,
};

#[derive(Default)]
struct RecordingCodeFactSink {
    writes: Vec<&'static str>,
    files: usize,
    symbols: usize,
    stale_symbols: usize,
    imports: usize,
    calls: usize,
    inheritance: usize,
    unresolved_targets: usize,
    chunks: usize,
}

impl CodeFactSink for RecordingCodeFactSink {
    fn delete_file_non_symbol_facts(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
    ) -> anyhow::Result<()> {
        self.writes.push("delete_non_symbols");
        Ok(())
    }

    fn delete_stale_file_symbols(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<usize> {
        self.writes.push("delete_stale_symbols");
        self.stale_symbols += current_symbol_ids.len();
        Ok(0)
    }

    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
        self.writes.push("symbols");
        self.symbols += symbols.len();
        Ok(symbols.len())
    }

    fn upsert_file(&mut self, _file: &IndexedFile) -> anyhow::Result<()> {
        self.writes.push("file");
        self.files += 1;
        Ok(())
    }

    fn upsert_imports(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        self.writes.push("imports");
        self.imports += imports.len();
        Ok(imports.len())
    }

    fn upsert_calls(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        self.writes.push("calls");
        self.calls += calls.len();
        self.unresolved_targets += calls
            .iter()
            .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
            .count();
        Ok(calls.len())
    }

    fn upsert_inheritance(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
        inheritance: &[InheritanceRelation],
    ) -> anyhow::Result<usize> {
        self.writes.push("inheritance");
        self.inheritance += inheritance.len();
        Ok(inheritance.len())
    }

    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
        self.writes.push("chunks");
        self.chunks += chunks.len();
        Ok(chunks.len())
    }
}

#[test]
fn library_writes_all_code_facts() {
    let project_id = "project-1";
    let rel = "src/lib.rs";
    let source = b"use std::fmt;\nfn caller() {\n    missing();\n}\n";
    let caller_id = Symbol::make_id(project_id, rel, "hash-1", "caller", "function", 14);
    let parse_result = ParseResult {
        symbols: vec![Symbol {
            id: caller_id.clone(),
            project_id: project_id.to_string(),
            file_path: rel.to_string(),
            name: "caller".to_string(),
            qualified_name: "caller".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 14,
            byte_end: 45,
            line_start: 2,
            line_end: 4,
            signature: Some("fn caller()".to_string()),
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: "hash-1".to_string(),
            content_hash: "hash-1".to_string(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }],
        imports: vec![ImportRelation {
            file_path: rel.to_string(),
            module_name: "std::fmt".to_string(),
        }],
        calls: vec![CallRelation::new(
            caller_id,
            "missing".to_string(),
            rel.to_string(),
            3,
        )],
        inheritance: Vec::new(),
        source: source.to_vec(),
    };

    let mut sink = RecordingCodeFactSink::default();
    let counts = write_parsed_file_facts(
        &mut sink,
        project_id,
        rel,
        "rust",
        "hash-1",
        source.len(),
        &parse_result,
    )
    .expect("write parsed file facts");

    assert_eq!(
        sink.writes,
        vec![
            "file",
            "symbols",
            "delete_stale_symbols",
            "delete_non_symbols",
            "imports",
            "calls",
            "inheritance",
            "chunks"
        ]
    );
    assert_eq!(sink.files, 1);
    assert_eq!(sink.symbols, 1);
    assert_eq!(sink.stale_symbols, 1);
    assert_eq!(sink.imports, 1);
    assert_eq!(sink.calls, 1);
    assert_eq!(sink.inheritance, 0);
    assert_eq!(sink.unresolved_targets, 1);
    assert_eq!(sink.chunks, 1);
    assert_eq!(counts.indexed_files, 1);
    assert_eq!(counts.symbols_indexed, 1);
    assert_eq!(counts.imports_indexed, 1);
    assert_eq!(counts.calls_indexed, 1);
    assert_eq!(counts.unresolved_targets_indexed, 1);
    assert_eq!(counts.chunks_indexed, 1);
}

#[test]
fn inheritance_pending_status_keeps_graph_only_owner() {
    use super::super::lifecycle::attach_projection_sync;
    use super::super::{IndexOutcome, IndexRequest};

    let mut outcome = IndexOutcome {
        project_id: "project-1".to_string(),
        indexed_file_paths: vec!["src/base.py".to_string()],
        ..IndexOutcome::default()
    };
    outcome.record_promotion_owners(["src/derived.py".to_string()]);
    attach_projection_sync(
        &mut outcome,
        &IndexRequest {
            project_root: std::path::PathBuf::from("/tmp/project"),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: true,
        },
    );
    let status = outcome.projection_sync.expect("projection status");
    assert_eq!(
        status.graph_file_paths,
        vec!["src/base.py".to_string(), "src/derived.py".to_string()]
    );
    assert_eq!(status.vector_file_paths, vec!["src/base.py".to_string()]);
    assert!(
        !status
            .vector_file_paths
            .contains(&"src/derived.py".to_string())
    );
}

mod serial_db {
    use super::super::super::IndexOutcome;
    use super::super::super::file::write_parsed_file_facts;
    use super::super::super::local_imports::resolve_local_import_inheritance;
    use super::super::super::sink::PostgresCodeFactSink;
    use crate::db;
    use crate::index::api;
    use crate::models::{
        CallTargetKind, HeritageKind, IndexedProject, InheritanceRelation, ParseResult, Symbol,
    };
    use std::path::Path;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_imported_base_promotes_after_base_indexes() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-promote-later");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        let before = inheritance_row(&mut conn, &project_id);
        assert_eq!(before.target_kind, "local_import");
        assert_eq!(before.target_module.as_deref(), Some("src/base.py"));

        write_base_symbol(&mut conn, &project_id, "hash-b");
        let owners =
            resolve_local_import_inheritance(&mut conn, &project_id, &["src/base.py".to_string()])
                .expect("promote after base");
        assert_eq!(owners, vec!["src/derived.py".to_string()]);
        let after = inheritance_row(&mut conn, &project_id);
        assert_eq!(after.target_kind, "symbol");
        assert!(after.target_symbol_id.is_some());
        assert!(after.target_module.is_none());
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_local_import_miss_stays_retryable() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-miss");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        resolve_local_import_inheritance(&mut conn, &project_id, &["src/derived.py".to_string()])
            .expect("miss");
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "local_import");
        assert_eq!(row.target_module.as_deref(), Some("src/base.py"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_rows_are_retained_per_content_hash_across_reindex() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-reindex");
        write_derived_pending(&mut conn, &project_id, "hash-d1");
        write_derived_without_heritage(&mut conn, &project_id, "hash-d2");
        // Superseded hash-d1 rows stay until content GC, not this reindex write.
        assert_eq!(count_inheritance_hash(&mut conn, &project_id, "hash-d1"), 1);
        assert_eq!(count_inheritance_hash(&mut conn, &project_id, "hash-d2"), 0);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_source_promotes_when_type_file_indexes() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-source-later");
        write_impl_pending(&mut conn, &project_id);
        write_type_symbol(&mut conn, &project_id);
        let owners =
            resolve_local_import_inheritance(&mut conn, &project_id, &["src/type.rs".to_string()])
                .expect("promote source");
        assert_eq!(owners, vec!["src/impls.rs".to_string()]);
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.source_kind, "symbol");
        assert!(row.source_symbol_id.is_some());
        assert_eq!(row.target_kind, "local_import");
        assert_eq!(row.target_module.as_deref(), Some("src/trait.rs"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_imported_base_resolves_when_base_already_indexed() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-base-first");
        write_base_symbol(&mut conn, &project_id, "hash-b");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        let owners = resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &["src/derived.py".to_string()],
        )
        .expect("resolve on derived write");
        assert_eq!(owners, vec!["src/derived.py".to_string()]);
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "symbol");
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_impl_source_resolves_when_type_already_indexed() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-type-first");
        write_type_symbol(&mut conn, &project_id);
        write_impl_pending(&mut conn, &project_id);
        resolve_local_import_inheritance(&mut conn, &project_id, &["src/impls.rs".to_string()])
            .expect("resolve impl source");
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.source_kind, "symbol");
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_keeps_independent_source_and_target_carriers() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-independent");
        write_impl_pending(&mut conn, &project_id);
        let before = inheritance_row(&mut conn, &project_id);
        assert_eq!(before.source_module.as_deref(), Some("src/type.rs"));
        assert_eq!(before.target_module.as_deref(), Some("src/trait.rs"));
        write_type_symbol(&mut conn, &project_id);
        resolve_local_import_inheritance(&mut conn, &project_id, &["src/type.rs".to_string()])
            .expect("promote source only");
        let after = inheritance_row(&mut conn, &project_id);
        assert_eq!(after.source_kind, "symbol");
        assert!(after.source_module.is_none());
        assert_eq!(after.target_kind, "local_import");
        assert_eq!(after.target_module.as_deref(), Some("src/trait.rs"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_promotion_dirties_owner_graph_pending() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-dirty");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        mark_owner_fully_synced(&mut conn, &project_id, "src/derived.py", "hash-d");
        write_base_symbol(&mut conn, &project_id, "hash-b");
        resolve_local_import_inheritance(&mut conn, &project_id, &["src/base.py".to_string()])
            .expect("promote");
        let project_uuid = db::id_param(&project_id).expect("uuid");
        let row = conn
            .query_one(
                "SELECT graph_synced, graph_sync_attempted_at IS NULL, vectors_synced
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = 'src/derived.py' AND content_hash = 'hash-d'",
                &[&project_uuid],
            )
            .expect("owner flags");
        let graph_synced: bool = row.get(0);
        let attempt_cleared: bool = row.get(1);
        let vectors_synced: bool = row.get(2);
        assert!(!graph_synced);
        assert!(attempt_cleared);
        assert!(vectors_synced);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_promotion_uses_active_content_only() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-active");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        write_base_symbol(&mut conn, &project_id, "hash-old");
        write_base_symbol(&mut conn, &project_id, "hash-new");
        resolve_local_import_inheritance(&mut conn, &project_id, &["src/base.py".to_string()])
            .expect("active hash-new");
        let promoted = inheritance_row(&mut conn, &project_id);
        assert_eq!(promoted.target_kind, "symbol");
        let project_uuid = db::id_param(&project_id).expect("uuid");
        let id: uuid::Uuid = conn
            .query_one(
                "SELECT target_symbol_id FROM code_inheritance WHERE project_id = $1",
                &[&project_uuid],
            )
            .expect("id")
            .get(0);
        let file_hash: String = conn
            .query_one(
                "SELECT file_content_hash FROM code_symbols WHERE id = $1",
                &[&id],
            )
            .expect("symbol hash")
            .get(0);
        assert_eq!(file_hash, "hash-new");
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_adoption_promotes_pending_consumer() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-adopt");
        write_derived_pending(&mut conn, &project_id, "hash-d");
        write_base_symbol(&mut conn, &project_id, "hash-b");
        mark_owner_fully_synced(&mut conn, &project_id, "src/base.py", "hash-b");
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        assert!(
            api::adopt_file_state(&mut conn, &machine_id, &project_id, "src/base.py", "hash-b")
                .expect("adopt provider")
        );
        let owners =
            resolve_local_import_inheritance(&mut conn, &project_id, &["src/base.py".to_string()])
                .expect("promote adopted provider");
        let mut outcome = IndexOutcome::new(&project_id);
        outcome.record_promotion_owners(owners);
        assert!(
            !outcome
                .vector_file_paths
                .iter()
                .any(|path| path == "src/base.py"),
            "adopted provider is not a vector path"
        );
        assert!(
            outcome
                .graph_file_paths
                .iter()
                .any(|path| path == "src/derived.py")
        );
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "symbol");
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
                let _ = cleanup(&mut conn, &self.project_id);
            }
        }
    }

    struct InheritanceRow {
        source_kind: String,
        source_symbol_id: Option<uuid::Uuid>,
        source_module: Option<String>,
        target_kind: String,
        target_symbol_id: Option<uuid::Uuid>,
        target_module: Option<String>,
    }

    fn seeded_project(prefix: &str) -> (postgres::Client, String, ProjectCleanup) {
        let database_url = crate::test_env::postgres_test_database_url("indexer inheritance tests");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let project_id = unique_id(prefix);
        cleanup(&mut conn, &project_id).expect("pre-clean");
        let guard = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        api::upsert_project_stats(
            &mut conn,
            &machine_id,
            &IndexedProject {
                id: project_id.clone(),
                root_path: format!("/tmp/{project_id}"),
                total_files: 1,
                total_symbols: 0,
                last_indexed_at: String::new(),
                index_duration_ms: 0,
                total_eligible_files: None,
            },
        )
        .expect("seed project");
        (conn, project_id, guard)
    }

    fn write_derived_pending(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        let rel = "src/derived.py";
        let kind = "class";
        let source_id = Symbol::make_id(project_id, rel, hash, "Derived", kind, 0);
        let parse = ParseResult {
            symbols: vec![type_symbol(
                project_id, rel, hash, "Derived", 0, "python", kind,
            )],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: vec![InheritanceRelation {
                source_symbol_id: Some(source_id),
                source_name: "Derived".to_string(),
                source_kind: CallTargetKind::Symbol,
                source_external_module: None,
                target_symbol_id: None,
                target_name: "Base".to_string(),
                target_kind: CallTargetKind::LocalImport,
                target_external_module: Some("src/base.py".to_string()),
                heritage_kind: HeritageKind::Inherits,
                file_path: rel.to_string(),
                content_hash: hash.to_string(),
                line: 2,
            }],
            source: b"from base import Base\nclass Derived(Base):\n    pass\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "python", hash, parse);
    }

    fn write_derived_without_heritage(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        let rel = "src/derived.py";
        let parse = ParseResult {
            symbols: vec![type_symbol(
                project_id, rel, hash, "Derived", 0, "python", "class",
            )],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: b"class Derived:\n    pass\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "python", hash, parse);
    }

    fn write_base_symbol(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        let rel = "src/base.py";
        let parse = ParseResult {
            symbols: vec![type_symbol(
                project_id, rel, hash, "Base", 0, "python", "class",
            )],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: b"class Base:\n    pass\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "python", hash, parse);
    }

    fn write_impl_pending(conn: &mut postgres::Client, project_id: &str) {
        let rel = "src/impls.rs";
        let hash = "hash-impl";
        let parse = ParseResult {
            symbols: Vec::new(),
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: vec![InheritanceRelation {
                source_symbol_id: None,
                source_name: "Type".to_string(),
                source_kind: CallTargetKind::LocalImport,
                source_external_module: Some("src/type.rs".to_string()),
                target_symbol_id: None,
                target_name: "Trait".to_string(),
                target_kind: CallTargetKind::LocalImport,
                target_external_module: Some("src/trait.rs".to_string()),
                heritage_kind: HeritageKind::Implements,
                file_path: rel.to_string(),
                content_hash: hash.to_string(),
                line: 4,
            }],
            source: b"impl Trait for Type {}\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "rust", hash, parse);
    }

    fn write_type_symbol(conn: &mut postgres::Client, project_id: &str) {
        let rel = "src/type.rs";
        let hash = "hash-type";
        let parse = ParseResult {
            symbols: vec![type_symbol(
                project_id, rel, hash, "Type", 0, "rust", "struct",
            )],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: b"pub struct Type;\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "rust", hash, parse);
    }

    fn write_facts(
        conn: &mut postgres::Client,
        project_id: &str,
        rel: &str,
        language: &str,
        hash: &str,
        parse: ParseResult,
    ) {
        let mut tx = conn.transaction().expect("tx");
        let mut sink = PostgresCodeFactSink::new(&mut tx, project_id, Path::new("/tmp/inherit"))
            .expect("sink");
        write_parsed_file_facts(
            &mut sink,
            project_id,
            rel,
            language,
            hash,
            parse.source.len(),
            &parse,
        )
        .expect("write facts");
        tx.commit().expect("commit");
    }

    fn type_symbol(
        project_id: &str,
        rel: &str,
        hash: &str,
        name: &str,
        byte_start: usize,
        language: &str,
        kind: &str,
    ) -> Symbol {
        Symbol {
            id: Symbol::make_id(project_id, rel, hash, name, kind, byte_start),
            project_id: project_id.to_string(),
            file_path: rel.to_string(),
            name: name.to_string(),
            qualified_name: name.to_string(),
            kind: kind.to_string(),
            language: language.to_string(),
            byte_start,
            byte_end: byte_start + name.len(),
            line_start: 1,
            line_end: 1,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: hash.to_string(),
            content_hash: hash.to_string(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    fn inheritance_row(conn: &mut postgres::Client, project_id: &str) -> InheritanceRow {
        let project_uuid = db::id_param(project_id).expect("uuid");
        let row = conn
            .query_one(
                "SELECT source_kind, source_symbol_id, NULLIF(source_external_module, ''),
                        target_kind, target_symbol_id, NULLIF(target_external_module, '')
                 FROM code_inheritance WHERE project_id = $1",
                &[&project_uuid],
            )
            .expect("load inheritance");
        InheritanceRow {
            source_kind: row.get(0),
            source_symbol_id: row.get(1),
            source_module: row.get(2),
            target_kind: row.get(3),
            target_symbol_id: row.get(4),
            target_module: row.get(5),
        }
    }

    fn count_inheritance(conn: &mut postgres::Client, project_id: &str) -> i64 {
        let project_uuid = db::id_param(project_id).expect("uuid");
        conn.query_one(
            "SELECT COUNT(*) FROM code_inheritance WHERE project_id = $1",
            &[&project_uuid],
        )
        .expect("count")
        .get(0)
    }

    fn count_inheritance_hash(conn: &mut postgres::Client, project_id: &str, hash: &str) -> i64 {
        let project_uuid = db::id_param(project_id).expect("uuid");
        conn.query_one(
            "SELECT COUNT(*) FROM code_inheritance WHERE project_id = $1 AND content_hash = $2",
            &[&project_uuid, &hash],
        )
        .expect("count hash")
        .get(0)
    }

    fn mark_owner_fully_synced(
        conn: &mut postgres::Client,
        project_id: &str,
        rel: &str,
        hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("uuid");
        conn.execute(
            "UPDATE code_indexed_files
             SET graph_synced = TRUE, vectors_synced = TRUE, graph_sync_attempted_at = NOW()
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_uuid, &rel, &hash],
        )
        .expect("mark synced");
    }

    fn cleanup(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = db::id_param(project_id)?;
        let mut tx = conn.transaction()?;
        tx.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&project_id],
        )?;
        tx.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&project_id],
        )?;
        tx.execute(
            "DELETE FROM code_inheritance WHERE project_id = $1",
            &[&project_id],
        )?;
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

    fn unique_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("epoch")
            .as_nanos();
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
        )
        .to_string()
    }
}
