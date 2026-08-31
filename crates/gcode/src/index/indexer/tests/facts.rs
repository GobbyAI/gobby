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
    use super::super::super::local_imports::{
        resolve_local_import_calls, resolve_local_import_inheritance,
        resolve_project_local_import_inheritance,
    };
    use super::super::super::sink::PostgresCodeFactSink;
    use crate::db;
    use crate::index::api;
    use crate::models::{
        CallRelation, CallTargetKind, HeritageKind, IndexedProject, InheritanceRelation,
        ParseResult, Symbol,
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
    fn inheritance_reexported_base_promotes_via_module_root_subtree() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-reexport-py");
        write_pending_heritage(
            &mut conn,
            &project_id,
            HeritageSeed {
                owner: "pkg/derived.py",
                hash: "hash-d",
                derived: "Derived",
                target: "Base",
                candidate: "pkg/__init__.py",
                language: "python",
                kind: "class",
                source: b"from pkg import Base\nclass Derived(Base):\n    pass\n",
            },
        );
        write_named_symbol(
            &mut conn,
            &project_id,
            SymbolSeed {
                rel: "pkg/impl.py",
                hash: "hash-b",
                name: "Base",
                kind: "class",
                language: "python",
                source: b"class Base:\n    pass\n",
            },
        );
        let owners = resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &["pkg/__init__.py".to_string()],
        )
        .expect("promote python reexport");
        assert_eq!(owners, vec!["pkg/derived.py".to_string()]);
        let after = inheritance_row(&mut conn, &project_id);
        assert_eq!(after.target_kind, "symbol");
        assert!(after.target_symbol_id.is_some());
        assert!(after.target_module.is_none());
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);

        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-reexport-rs");
        write_pending_heritage(
            &mut conn,
            &project_id,
            HeritageSeed {
                owner: "src/store/client.rs",
                hash: "hash-c",
                derived: "Client",
                target: "Store",
                candidate: "src/store/mod.rs",
                language: "rust",
                kind: "type",
                source: b"impl Store for Client {}\n",
            },
        );
        write_named_symbol(
            &mut conn,
            &project_id,
            SymbolSeed {
                rel: "src/store/types.rs",
                hash: "hash-t",
                name: "Store",
                kind: "type",
                language: "rust",
                source: b"pub trait Store {}\n",
            },
        );
        let repair = resolve_project_local_import_inheritance(&mut conn, &project_id)
            .expect("promote rust reexport via repair");
        assert_eq!(repair.owners, vec!["src/store/client.rs".to_string()]);
        assert_eq!(repair.pending, 1);
        assert_eq!(repair.resolved, 1);
        let after = inheritance_row(&mut conn, &project_id);
        assert_eq!(after.target_kind, "symbol");
        assert!(after.target_symbol_id.is_some());
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_ambiguous_subtree_stays_retryable() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-ambiguous");
        write_pending_heritage(
            &mut conn,
            &project_id,
            HeritageSeed {
                owner: "pkg/derived.py",
                hash: "hash-d",
                derived: "Derived",
                target: "Base",
                candidate: "pkg/__init__.py",
                language: "python",
                kind: "class",
                source: b"from pkg import Base\nclass Derived(Base):\n    pass\n",
            },
        );
        write_named_symbol(
            &mut conn,
            &project_id,
            SymbolSeed {
                rel: "pkg/a.py",
                hash: "hash-a",
                name: "Base",
                kind: "class",
                language: "python",
                source: b"class Base:\n    pass\n",
            },
        );
        write_named_symbol(
            &mut conn,
            &project_id,
            SymbolSeed {
                rel: "pkg/b.py",
                hash: "hash-b",
                name: "Base",
                kind: "class",
                language: "python",
                source: b"class Base:\n    pass\n",
            },
        );
        resolve_local_import_inheritance(&mut conn, &project_id, &["pkg/__init__.py".to_string()])
            .expect("ambiguous subtree");
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "local_import");
        assert_eq!(row.target_module.as_deref(), Some("pkg/__init__.py"));
        assert_eq!(count_inheritance(&mut conn, &project_id), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn inheritance_plain_file_candidate_does_not_widen() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-plain");
        write_pending_heritage(
            &mut conn,
            &project_id,
            HeritageSeed {
                owner: "pkg/derived.py",
                hash: "hash-d",
                derived: "Derived",
                target: "Base",
                candidate: "pkg/api.py",
                language: "python",
                kind: "class",
                source: b"from pkg.api import Base\nclass Derived(Base):\n    pass\n",
            },
        );
        write_named_symbol(
            &mut conn,
            &project_id,
            SymbolSeed {
                rel: "pkg/impl.py",
                hash: "hash-b",
                name: "Base",
                kind: "class",
                language: "python",
                source: b"class Base:\n    pass\n",
            },
        );
        resolve_local_import_inheritance(&mut conn, &project_id, &["pkg/api.py".to_string()])
            .expect("plain file miss");
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "local_import");
        assert_eq!(row.target_module.as_deref(), Some("pkg/api.py"));

        let (mut conn, project_id, _cleanup) = seeded_project("gcode-inherit-nested");
        write_pending_heritage(
            &mut conn,
            &project_id,
            HeritageSeed {
                owner: "pkg/derived.py",
                hash: "hash-d",
                derived: "Derived",
                target: "Base",
                candidate: "pkg/__init__.py",
                language: "python",
                kind: "class",
                source: b"from pkg import Base\nclass Derived(Base):\n    pass\n",
            },
        );
        let helper_rel = "pkg/impl.py";
        let helper_hash = "hash-h";
        let helper = type_symbol(
            &project_id,
            helper_rel,
            helper_hash,
            "Helper",
            0,
            "python",
            "class",
        );
        let mut method = type_symbol(
            &project_id,
            helper_rel,
            helper_hash,
            "Base",
            20,
            "python",
            "method",
        );
        method.parent_symbol_id = Some(helper.id.clone());
        let parse = ParseResult {
            symbols: vec![helper, method],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: b"class Helper:\n    def Base(self):\n        pass\n".to_vec(),
        };
        write_facts(
            &mut conn,
            &project_id,
            helper_rel,
            "python",
            helper_hash,
            parse,
        );
        resolve_local_import_inheritance(&mut conn, &project_id, &["pkg/__init__.py".to_string()])
            .expect("nested method miss");
        let row = inheritance_row(&mut conn, &project_id);
        assert_eq!(row.target_kind, "local_import");
        assert_eq!(row.target_module.as_deref(), Some("pkg/__init__.py"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn call_through_imported_type_promotes_to_its_method() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-type-member-hit");
        let bar = foo_symbol(&project_id, "Bar", 0, "type", None);
        let mut bar_new = foo_symbol(&project_id, "new", 20, "method", Some(&bar));
        bar_new.qualified_name = "Bar::new".to_string();
        let method_id = bar_new.id.clone();
        write_foo_symbols(&mut conn, &project_id, vec![bar, bar_new]);
        write_type_member_call(&mut conn, &project_id);

        let resolved =
            resolve_local_import_calls(&mut conn, &project_id, &["src/app.rs".to_string()])
                .expect("promote Bar::new");
        assert_eq!(resolved, 1);
        let row = call_row(&mut conn, &project_id, "new");
        assert_eq!(row.target_kind, "symbol");
        assert_eq!(row.symbol_id.map(|id| id.to_string()), Some(method_id));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn call_through_imported_type_never_binds_free_function_or_other_type() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-type-member-miss");
        let bar = foo_symbol(&project_id, "Bar", 0, "type", None);
        let free_new = foo_symbol(&project_id, "new", 20, "function", None);
        let baz = foo_symbol(&project_id, "Baz", 40, "type", None);
        let mut baz_new = foo_symbol(&project_id, "new", 60, "method", Some(&baz));
        baz_new.qualified_name = "Baz::new".to_string();
        write_foo_symbols(&mut conn, &project_id, vec![bar, free_new, baz, baz_new]);
        write_type_member_call(&mut conn, &project_id);

        let resolved =
            resolve_local_import_calls(&mut conn, &project_id, &["src/app.rs".to_string()])
                .expect("resolve Bar::new miss");
        assert_eq!(resolved, 0);
        let row = call_row(&mut conn, &project_id, "new");
        assert_eq!(row.target_kind, "unresolved");
        assert!(row.symbol_id.is_none());
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
            api::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                "src/base.py",
                "hash-b",
                Path::new("/tmp/provider-view"),
                api::IndexWriteMode::Overlay,
            )
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
                indexer_version: None,
            },
            api::IndexWriteMode::Overlay,
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

    struct HeritageSeed<'a> {
        owner: &'a str,
        hash: &'a str,
        derived: &'a str,
        target: &'a str,
        candidate: &'a str,
        language: &'a str,
        kind: &'a str,
        source: &'a [u8],
    }

    fn write_pending_heritage(
        conn: &mut postgres::Client,
        project_id: &str,
        seed: HeritageSeed<'_>,
    ) {
        let source_id = Symbol::make_id(
            project_id,
            seed.owner,
            seed.hash,
            seed.derived,
            seed.kind,
            0,
        );
        let parse = ParseResult {
            symbols: vec![type_symbol(
                project_id,
                seed.owner,
                seed.hash,
                seed.derived,
                0,
                seed.language,
                seed.kind,
            )],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: vec![InheritanceRelation {
                source_symbol_id: Some(source_id),
                source_name: seed.derived.to_string(),
                source_kind: CallTargetKind::Symbol,
                source_external_module: None,
                target_symbol_id: None,
                target_name: seed.target.to_string(),
                target_kind: CallTargetKind::LocalImport,
                target_external_module: Some(seed.candidate.to_string()),
                heritage_kind: HeritageKind::Inherits,
                file_path: seed.owner.to_string(),
                content_hash: seed.hash.to_string(),
                line: 2,
            }],
            source: seed.source.to_vec(),
        };
        write_facts(
            conn,
            project_id,
            seed.owner,
            seed.language,
            seed.hash,
            parse,
        );
    }

    struct SymbolSeed<'a> {
        rel: &'a str,
        hash: &'a str,
        name: &'a str,
        kind: &'a str,
        language: &'a str,
        source: &'a [u8],
    }

    fn write_named_symbol(conn: &mut postgres::Client, project_id: &str, seed: SymbolSeed<'_>) {
        let symbol = type_symbol(
            project_id,
            seed.rel,
            seed.hash,
            seed.name,
            0,
            seed.language,
            seed.kind,
        );
        let parse = ParseResult {
            symbols: vec![symbol],
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: seed.source.to_vec(),
        };
        write_facts(conn, project_id, seed.rel, seed.language, seed.hash, parse);
    }

    const FOO_REL: &str = "src/foo.rs";
    const FOO_HASH: &str = "hash-foo";

    fn foo_symbol(
        project_id: &str,
        name: &str,
        byte_start: usize,
        kind: &str,
        parent: Option<&Symbol>,
    ) -> Symbol {
        let mut symbol = type_symbol(
            project_id, FOO_REL, FOO_HASH, name, byte_start, "rust", kind,
        );
        symbol.parent_symbol_id = parent.map(|parent| parent.id.clone());
        symbol
    }

    fn write_foo_symbols(conn: &mut postgres::Client, project_id: &str, symbols: Vec<Symbol>) {
        let parse = ParseResult {
            symbols,
            imports: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
            source: b"pub struct Bar;\nimpl Bar { pub fn new() -> Self { Bar } }\n".to_vec(),
        };
        write_facts(conn, project_id, FOO_REL, "rust", FOO_HASH, parse);
    }

    /// `src/app.rs` calling `Bar::new()` through `use crate::foo::Bar;`.
    fn write_type_member_call(conn: &mut postgres::Client, project_id: &str) {
        let rel = "src/app.rs";
        let hash = "hash-app";
        let go = type_symbol(project_id, rel, hash, "go", 0, "rust", "function");
        let call = CallRelation::new(go.id.clone(), "new".to_string(), rel.to_string(), 4)
            .with_local_type_member_target(
                "new".to_string(),
                "Bar",
                vec!["src/foo.rs".to_string(), "src/foo/mod.rs".to_string()],
            );
        let parse = ParseResult {
            symbols: vec![go],
            imports: Vec::new(),
            calls: vec![call],
            inheritance: Vec::new(),
            source: b"use crate::foo::Bar;\nfn go() {\n    Bar::new();\n}\n".to_vec(),
        };
        write_facts(conn, project_id, rel, "rust", hash, parse);
    }

    struct CallRow {
        target_kind: String,
        symbol_id: Option<uuid::Uuid>,
    }

    fn call_row(conn: &mut postgres::Client, project_id: &str, callee_name: &str) -> CallRow {
        let project_uuid = db::id_param(project_id).expect("uuid");
        let row = conn
            .query_one(
                "SELECT callee_target_kind, callee_symbol_id
                 FROM code_calls WHERE project_id = $1 AND callee_name = $2",
                &[&project_uuid, &callee_name],
            )
            .expect("load call");
        CallRow {
            target_kind: row.get(0),
            symbol_id: row.get(1),
        }
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
                project_id, rel, hash, "Type", 0, "rust", "type",
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
        let mut sink = PostgresCodeFactSink::new(
            &mut tx,
            project_id,
            Path::new("/tmp/inherit"),
            api::IndexWriteMode::Overlay,
        )
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
