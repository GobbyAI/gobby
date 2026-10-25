use self::serial_db::{ProjectCleanup, cleanup, local_machine_id, unique_id};
use super::super::file::write_parsed_file_facts;
use super::super::sink::CodeFactSink;
use super::super::{IndexOptions, IndexRequest, index_files};
use super::fixtures::{git, write_file};
use super::serial_db::machine_context;
use crate::config::ProjectIndexScope;
use crate::db;
use crate::db::GraphFileFacts;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, InheritanceRelation,
    ParseResult, Symbol,
};

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn candidate_imports_keep_unchanged_providers() {
    let database_url = crate::test_env::postgres_test_database_url("candidate import providers");
    let mut conn = db::connect_readwrite(&database_url).expect("connect");
    let base = tempfile::tempdir().expect("create temp base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent_root = base.path().join("parent");
    std::fs::create_dir_all(&parent_root).expect("create parent root");
    let overlay_root = base.path().join("overlay");

    // The provider is unchanged in both trees. Only the importer diverges, by a
    // commit on the overlay branch, so the provider is not a candidate.
    write_file(
        &parent_root,
        "src/util.js",
        b"export function helper() { return 1; }\n",
    );
    write_file(
        &parent_root,
        "src/app.js",
        b"import { helper } from './util.js';\nexport function run() { return helper(); }\n",
    );
    git(&parent_root, &hooks, &["init", "-q"]);
    git(&parent_root, &hooks, &["add", "."]);
    git(&parent_root, &hooks, &["commit", "-q", "-m", "seed"]);
    git(
        &parent_root,
        &hooks,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "wt",
            overlay_root.to_str().expect("utf-8 overlay path"),
        ],
    );
    write_file(
        &overlay_root,
        "src/app.js",
        b"import { helper } from './util.js';\nexport function run() { return helper() + 1; }\n",
    );
    git(
        &overlay_root,
        &hooks,
        &["commit", "-q", "-am", "importer change"],
    );

    let parent_project_id = unique_id("gcode-candidate-provider-parent");
    let overlay_project_id = unique_id("gcode-candidate-provider-overlay");
    cleanup(&mut conn, &parent_project_id).expect("pre-clean parent");
    cleanup(&mut conn, &overlay_project_id).expect("pre-clean overlay");
    let _parent_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: parent_project_id.clone(),
    };
    let _overlay_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: overlay_project_id.clone(),
    };
    let machine_id = local_machine_id();
    super::serial_db::seed_primary_checkout(&mut conn, &parent_project_id, &parent_root)
        .expect("seed parent checkout");

    let parent_ctx = machine_context(&database_url, &parent_project_id, &parent_root, &machine_id);
    index_files(
        IndexRequest {
            project_root: parent_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &parent_ctx,
        IndexOptions::default(),
    )
    .expect("index parent");

    let helper_id: uuid::Uuid = conn
        .query_one(
            "SELECT id FROM code_symbols WHERE project_id = $1 AND name = 'helper'",
            &[&db::id_param(&parent_project_id).expect("parent id")],
        )
        .expect("parent provider symbol")
        .get(0);
    let parent_call = conn
        .query_one(
            "SELECT callee_target_kind, callee_symbol_id FROM code_calls
             WHERE project_id = $1 AND callee_name = 'helper'",
            &[&db::id_param(&parent_project_id).expect("parent id")],
        )
        .expect("parent importer call");
    let parent_kind: String = parent_call.get(0);
    let parent_symbol: Option<uuid::Uuid> = parent_call.get(1);
    assert_eq!(
        (parent_kind.as_str(), parent_symbol),
        ("symbol", Some(helper_id)),
        "precondition: the parent resolves its importer call to the provider it serves"
    );

    let mut overlay_ctx = machine_context(
        &database_url,
        &overlay_project_id,
        &overlay_root,
        &machine_id,
    );
    overlay_ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: overlay_project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_project_id.clone(),
        parent_root: parent_root.clone(),
    };
    index_files(
        IndexRequest {
            project_root: overlay_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &overlay_ctx,
        IndexOptions::default(),
    )
    .expect("overlay index");

    let call = conn
        .query_one(
            "SELECT callee_target_kind, callee_symbol_id FROM code_calls
             WHERE project_id = $1 AND callee_name = 'helper'",
            &[&db::id_param(&overlay_project_id).expect("overlay id")],
        )
        .expect("overlay importer call");
    let target_kind: String = call.get(0);
    let symbol_id: Option<uuid::Uuid> = call.get(1);
    assert_eq!(
        (target_kind.as_str(), symbol_id),
        ("symbol", Some(helper_id)),
        "the importer's call resolves to the unchanged provider the parent serves"
    );
}

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

    fn read_version_facts(
        &mut self,
        _project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) -> anyhow::Result<GraphFileFacts> {
        Ok(GraphFileFacts {
            file_path: file_path.to_string(),
            content_hash: content_hash.to_string(),
            imports: Vec::new(),
            definitions: Vec::new(),
            calls: Vec::new(),
            inheritance: Vec::new(),
        })
    }

    fn dirty_version_sync(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        _content_hash: &str,
        _vectors: bool,
        _graph: bool,
    ) -> anyhow::Result<()> {
        self.writes.push("dirty_version_sync");
        Ok(())
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
            "chunks",
            "dirty_version_sync"
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
    use super::super::super::file::{write_content_only_file_facts, write_parsed_file_facts};
    use super::super::super::local_imports::{
        resolve_local_import_calls, resolve_local_import_inheritance,
        resolve_project_local_import_inheritance,
    };
    use super::super::super::sink::PostgresCodeFactSink;
    use super::super::super::{IndexOptions, IndexRequest, index_files};
    use super::super::fixtures::{git, git_output, write_file};
    use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
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
        let owners = resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/base.py".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/derived.py".to_string()],
        )
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
            &local_machine_id(),
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
        let repair =
            resolve_project_local_import_inheritance(&mut conn, &project_id, &local_machine_id())
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["pkg/__init__.py".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["pkg/api.py".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["pkg/__init__.py".to_string()],
        )
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

        let resolved = resolve_local_import_calls(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/app.rs".to_string()],
        )
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

        let resolved = resolve_local_import_calls(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/app.rs".to_string()],
        )
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
        let owners = resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/type.rs".to_string()],
        )
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
            &local_machine_id(),
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/impls.rs".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/type.rs".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/base.py".to_string()],
        )
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
        resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/base.py".to_string()],
        )
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
            api::file_state::adopt_file_state(
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
        let owners = resolve_local_import_inheritance(
            &mut conn,
            &project_id,
            &local_machine_id(),
            &["src/base.py".to_string()],
        )
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

    pub(super) struct ProjectCleanup {
        pub(super) database_url: String,
        pub(super) project_id: String,
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

    pub(super) fn local_machine_id() -> String {
        gobby_core::machine::read_local_machine_id().expect("read machine id")
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
            true,
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
            &local_machine_id(),
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

    pub(super) fn cleanup(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
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

    pub(super) fn unique_id(prefix: &str) -> String {
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

    fn indexed_file_row(
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) -> crate::models::IndexedFile {
        crate::models::IndexedFile {
            id: crate::models::IndexedFile::make_id(project_id, file_path, content_hash),
            project_id: project_id.to_string(),
            file_path: file_path.to_string(),
            language: "rust".to_string(),
            content_hash: content_hash.to_string(),
            symbol_count: 0,
            byte_size: 0,
            indexed_at: String::new(),
        }
    }

    fn selector_git_blob(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
    ) -> Option<String> {
        let project_id = db::id_param(project_id).expect("project uuid");
        conn.query_one(
            "SELECT git_blob_id FROM code_indexed_file_states
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )
        .expect("selector row")
        .get(0)
    }

    fn selector_row_xmin(conn: &mut postgres::Client, project_id: &str, file_path: &str) -> String {
        let project_id = db::id_param(project_id).expect("project uuid");
        conn.query_one(
            "SELECT xmin::text FROM code_indexed_file_states
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )
        .expect("selector row")
        .get(0)
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn unchanged_reindex_issues_no_selector_updates() {
        let database_url =
            crate::test_env::postgres_test_database_url("unchanged reindex selectors");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        std::fs::create_dir_all(&hooks).expect("create hooks dir");
        let project_root = base.path().join("repo");
        std::fs::create_dir_all(&project_root).expect("create project root");
        let clean = "src/clean.rs";
        write_file(&project_root, clean, b"pub fn clean() {}\n");
        git(&project_root, &hooks, &["init", "-q"]);
        git(&project_root, &hooks, &["add", "."]);
        git(&project_root, &hooks, &["commit", "-q", "-m", "seed"]);

        let project_id = unique_id("gcode-unchanged-reindex");
        cleanup(&mut conn, &project_id).expect("pre-clean");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        super::super::serial_db::seed_primary_checkout(&mut conn, &project_id, &project_root)
            .expect("seed primary checkout");
        let ctx = Context {
            database_url,
            project_root: project_root.clone(),
            project_id: project_id.clone(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        };
        let request = |full: bool| IndexRequest {
            project_root: project_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full,
            require_cpp_semantics: false,
            sync_projections: false,
        };
        index_files(request(true), &ctx, IndexOptions::default()).expect("first index");
        let before = selector_row_xmin(&mut conn, &project_id, clean);

        index_files(request(false), &ctx, IndexOptions::default()).expect("unchanged reindex");

        assert_eq!(
            selector_row_xmin(&mut conn, &project_id, clean),
            before,
            "an unchanged selector must not be rewritten"
        );
    }

    fn mark_content_synced(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_id = db::id_param(project_id).expect("project uuid");
        conn.execute(
            "UPDATE code_indexed_files SET graph_synced = TRUE, vectors_synced = TRUE
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_id, &file_path, &content_hash],
        )
        .expect("mark content synced");
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn clean_selector_records_git_blob() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-pin-clean-blob");
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        let rel = "src/clean.rs";
        let file = indexed_file_row(&project_id, rel, "hash-clean");
        api::upsert_file(&mut conn, &file).expect("insert indexed file");
        api::file_state::upsert_file_state(
            &mut conn,
            &machine_id,
            &file,
            Path::new("/tmp/clean"),
            api::IndexWriteMode::Overlay,
        )
        .expect("insert selector");

        assert!(
            api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-clean",
                Some("blob-clean"),
            )
            .expect("record clean blob")
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel).as_deref(),
            Some("blob-clean")
        );

        assert!(
            api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-clean",
                None,
            )
            .expect("clear dirty blob")
        );
        assert_eq!(selector_git_blob(&mut conn, &project_id, rel), None);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn selector_blob_follows_every_write_outcome() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-pin-blob-outcomes");
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        let rel = "src/outcome.rs";
        let root = Path::new("/tmp/outcome");

        // Fresh write: only the selector's current content hash can set a blob.
        let v1 = indexed_file_row(&project_id, rel, "hash-v1");
        api::upsert_file(&mut conn, &v1).expect("insert v1");
        api::file_state::upsert_file_state(
            &mut conn,
            &machine_id,
            &v1,
            root,
            api::IndexWriteMode::Overlay,
        )
        .expect("fresh selector");
        assert!(
            api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-v1",
                Some("blob-v1"),
            )
            .expect("fresh blob")
        );
        let before = selector_row_xmin(&mut conn, &project_id, rel);
        assert!(
            !api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-v1",
                Some("blob-v1"),
            )
            .expect("unchanged blob")
        );
        assert_eq!(selector_row_xmin(&mut conn, &project_id, rel), before);
        assert!(
            !api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-stale",
                Some("blob-stale"),
            )
            .expect("stale hash write")
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel).as_deref(),
            Some("blob-v1")
        );

        // Adopted with unchanged content: the carried-over blob ID survives.
        mark_content_synced(&mut conn, &project_id, rel, "hash-v1");
        assert!(
            api::file_state::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-v1",
                root,
                api::IndexWriteMode::Overlay,
            )
            .expect("adopt unchanged content")
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel).as_deref(),
            Some("blob-v1")
        );

        // Overwrite with changed content: the migration trigger clears the carried-over blob ID.
        let v2 = indexed_file_row(&project_id, rel, "hash-v2");
        api::upsert_file(&mut conn, &v2).expect("insert v2");
        api::file_state::upsert_file_state(
            &mut conn,
            &machine_id,
            &v2,
            root,
            api::IndexWriteMode::Overlay,
        )
        .expect("overwrite selector");
        assert_eq!(selector_git_blob(&mut conn, &project_id, rel), None);

        // Skipped back-fill: an unchanged clean file fills the missing blob ID once.
        assert!(
            api::file_state::set_selector_git_blob(
                &mut conn,
                &machine_id,
                &project_id,
                rel,
                "hash-v2",
                Some("blob-v2"),
            )
            .expect("skipped back-fill")
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel).as_deref(),
            Some("blob-v2")
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn indexed_selectors_record_blob_only_for_clean_tracked_paths() {
        let database_url = crate::test_env::postgres_test_database_url("indexer blob callers");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        std::fs::create_dir_all(&hooks).expect("create hooks dir");
        let project_root = base.path().join("repo");
        std::fs::create_dir_all(&project_root).expect("create project root");
        let clean = "src/clean.rs";
        let dirty = "src/dirty.rs";
        let untracked = "src/untracked.rs";
        write_file(&project_root, clean, b"pub fn clean() {}\n");
        write_file(&project_root, dirty, b"pub fn dirty_before() {}\n");
        git(&project_root, &hooks, &["init", "-q"]);
        git(&project_root, &hooks, &["add", "."]);
        git(&project_root, &hooks, &["commit", "-q", "-m", "seed"]);
        let clean_spec = format!("HEAD:{clean}");
        let clean_blob = git_output(&project_root, &hooks, &["rev-parse", clean_spec.as_str()])
            .trim()
            .to_string();
        // Only the clean file still matches HEAD: the tracked file is edited
        // after the commit and the other file is never added.
        write_file(&project_root, dirty, b"pub fn dirty_after() {}\n");
        write_file(&project_root, untracked, b"pub fn untracked() {}\n");

        let project_id = unique_id("gcode-blob-callers");
        cleanup(&mut conn, &project_id).expect("pre-clean");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        super::super::serial_db::seed_primary_checkout(&mut conn, &project_id, &project_root)
            .expect("seed primary checkout");
        let ctx = Context {
            database_url,
            project_root: project_root.clone(),
            project_id: project_id.clone(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        };
        let outcome = index_files(
            IndexRequest {
                project_root: project_root.clone(),
                path_filter: None,
                explicit_files: Vec::new(),
                full: true,
                require_cpp_semantics: false,
                sync_projections: false,
            },
            &ctx,
            IndexOptions::default(),
        )
        .expect("index fixture repo");
        assert_eq!(
            outcome.indexed_files, 3,
            "all three fixture files must be indexed"
        );

        assert_eq!(
            selector_git_blob(&mut conn, &project_id, clean).as_deref(),
            Some(clean_blob.as_str()),
            "a clean tracked selector records its HEAD blob"
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, dirty),
            None,
            "a dirty tracked selector does not qualify"
        );
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, untracked),
            None,
            "an untracked selector does not qualify"
        );
    }

    fn init_repo(root: &Path, hooks: &Path) {
        std::fs::create_dir_all(hooks).expect("create hooks dir");
        git(root, hooks, &["init", "-q"]);
    }

    fn commit_all(root: &Path, hooks: &Path) {
        git(root, hooks, &["add", "."]);
        git(root, hooks, &["commit", "-q", "-m", "fixture"]);
    }

    fn head_blob(root: &Path, hooks: &Path, rel: &str) -> String {
        let spec = format!("HEAD:{rel}");
        git_output(root, hooks, &["rev-parse", spec.as_str()])
            .trim()
            .to_string()
    }

    fn selector_context(
        database_url: String,
        root: &Path,
        project_id: &str,
        index_scope: ProjectIndexScope,
    ) -> Context {
        Context {
            database_url,
            project_root: root.to_path_buf(),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope,
        }
    }

    fn selector_request(root: &Path, full: bool) -> IndexRequest {
        IndexRequest {
            project_root: root.to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full,
            require_cpp_semantics: false,
            sync_projections: false,
        }
    }

    fn seed_project_stats(conn: &mut postgres::Client, machine_id: &str, project_id: &str) {
        api::upsert_project_stats(
            conn,
            machine_id,
            &IndexedProject {
                id: project_id.to_string(),
                root_path: format!("/tmp/{project_id}"),
                total_files: 1,
                total_symbols: 0,
                last_indexed_at: String::new(),
                index_duration_ms: 0,
                total_eligible_files: None,
                indexer_version: None,
            },
            api::IndexWriteMode::Overlay,
            true,
        )
        .expect("seed project stats");
    }

    // The parent must look indexed on this machine, with a diverging version of
    // the same path, before an overlay run validates it.
    fn seed_overlay_parent(
        conn: &mut postgres::Client,
        machine_id: &str,
        parent_project_id: &str,
        rel: &str,
    ) {
        seed_project_stats(conn, machine_id, parent_project_id);
        let parent_file = indexed_file_row(parent_project_id, rel, "parent-old-hash");
        api::upsert_file(conn, &parent_file).expect("seed parent content version");
        api::file_state::upsert_file_state(
            conn,
            machine_id,
            &parent_file,
            Path::new(&format!("/tmp/{parent_project_id}")),
            api::IndexWriteMode::Overlay,
        )
        .expect("seed parent selector");
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn selector_blob_backfills_on_skipped_primary_file() {
        let database_url = crate::test_env::postgres_test_database_url("indexer blob skip");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        let root = base.path().join("repo");
        std::fs::create_dir_all(&root).expect("create project root");
        let rel = "src/skip.rs";
        write_file(&root, rel, b"pub fn skip_v1() {}\n");
        init_repo(&root, &hooks);
        commit_all(&root, &hooks);
        // The first index sees an edit that is not committed yet, so its
        // selector is written without a blob.
        write_file(&root, rel, b"pub fn skip_v2() {}\n");

        let project_id = unique_id("gcode-blob-primary-skip");
        cleanup(&mut conn, &project_id).expect("pre-clean");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        super::super::serial_db::seed_primary_checkout(&mut conn, &project_id, &root)
            .expect("seed primary checkout");
        let ctx = selector_context(database_url, &root, &project_id, ProjectIndexScope::Single);
        index_files(selector_request(&root, true), &ctx, IndexOptions::default())
            .expect("full index of the dirty file");
        assert_eq!(selector_git_blob(&mut conn, &project_id, rel), None);

        // Committing the edit makes the unchanged selector match a clean HEAD.
        commit_all(&root, &hooks);
        let outcome = index_files(
            selector_request(&root, false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("incremental index of the committed file");
        assert_eq!(outcome.indexed_files, 0, "an unchanged selector is skipped");
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel),
            Some(head_blob(&root, &hooks, rel)),
            "the skip path back-fills the clean HEAD blob"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn selector_blob_backfills_on_adopted_primary_file() {
        let database_url = crate::test_env::postgres_test_database_url("indexer blob adopt");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        let root = base.path().join("repo");
        std::fs::create_dir_all(&root).expect("create project root");
        let rel = "src/adopt.rs";
        let bytes = b"pub fn adopt_v1() {}\n";
        write_file(&root, rel, bytes);
        init_repo(&root, &hooks);
        commit_all(&root, &hooks);
        let content_hash = crate::index::hasher::content_hash(bytes);

        let project_id = unique_id("gcode-blob-primary-adopt");
        cleanup(&mut conn, &project_id).expect("pre-clean");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        super::super::serial_db::seed_primary_checkout(&mut conn, &project_id, &root)
            .expect("seed primary checkout");
        let ctx = selector_context(database_url, &root, &project_id, ProjectIndexScope::Single);
        index_files(selector_request(&root, true), &ctx, IndexOptions::default())
            .expect("full index of the committed file");
        mark_content_synced(&mut conn, &project_id, rel, &content_hash);
        // Dropping this machine's selector makes the next run see the file as
        // stale, so it adopts the synced content version instead of parsing it.
        let project_uuid = db::id_param(&project_id).expect("project uuid");
        conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1 AND file_path = $2",
            &[&project_uuid, &rel],
        )
        .expect("drop selector");

        let outcome = index_files(
            selector_request(&root, false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("adoption run");
        assert_eq!(outcome.indexed_files, 0, "adoption must not re-parse");
        assert_eq!(outcome.skipped_files, 1);
        assert_eq!(
            selector_git_blob(&mut conn, &project_id, rel),
            Some(head_blob(&root, &hooks, rel)),
            "the adopt path back-fills the clean HEAD blob"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn selector_blob_backfills_on_skipped_overlay_file() {
        let database_url = crate::test_env::postgres_test_database_url("indexer overlay blob skip");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        let overlay_root = base.path().join("overlay");
        std::fs::create_dir_all(&overlay_root).expect("create overlay root");
        let rel = "src/lib.rs";
        let bytes = b"pub fn overlay_skip() {}\n";
        write_file(&overlay_root, rel, bytes);
        init_repo(&overlay_root, &hooks);
        commit_all(&overlay_root, &hooks);
        let content_hash = crate::index::hasher::content_hash(bytes);

        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        let overlay_project_id = unique_id("gcode-blob-overlay-skip");
        let parent_project_id = unique_id("gcode-blob-overlay-skip-parent");
        cleanup(&mut conn, &overlay_project_id).expect("pre-clean overlay");
        cleanup(&mut conn, &parent_project_id).expect("pre-clean parent");
        let _overlay_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: overlay_project_id.clone(),
        };
        let _parent_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: parent_project_id.clone(),
        };
        seed_overlay_parent(&mut conn, &machine_id, &parent_project_id, rel);
        // The overlay selector already carries the current content, so the run
        // takes the overlay skip branch.
        seed_project_stats(&mut conn, &machine_id, &overlay_project_id);
        let overlay_file = indexed_file_row(&overlay_project_id, rel, &content_hash);
        api::upsert_file(&mut conn, &overlay_file).expect("seed overlay content version");
        api::file_state::upsert_file_state(
            &mut conn,
            &machine_id,
            &overlay_file,
            &overlay_root,
            api::IndexWriteMode::Overlay,
        )
        .expect("seed overlay selector");

        let ctx = selector_context(
            database_url,
            &overlay_root,
            &overlay_project_id,
            ProjectIndexScope::Overlay {
                overlay_project_id: overlay_project_id.clone(),
                overlay_root: overlay_root.clone(),
                parent_project_id: parent_project_id.clone(),
                parent_root: std::path::PathBuf::from(format!("/tmp/{parent_project_id}")),
            },
        );
        let outcome = index_files(
            selector_request(&overlay_root, false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("overlay skip run");
        assert_eq!(outcome.indexed_files, 0, "an unchanged overlay is skipped");
        assert_eq!(
            selector_git_blob(&mut conn, &overlay_project_id, rel),
            Some(head_blob(&overlay_root, &hooks, rel)),
            "the overlay skip path back-fills the clean HEAD blob"
        );
        let before = selector_row_xmin(&mut conn, &overlay_project_id, rel);
        index_files(
            selector_request(&overlay_root, false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("unchanged overlay skip run");
        assert_eq!(
            selector_row_xmin(&mut conn, &overlay_project_id, rel),
            before,
            "an unchanged overlay selector must not be rewritten"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn selector_blob_backfills_on_adopted_overlay_file() {
        let database_url =
            crate::test_env::postgres_test_database_url("indexer overlay blob adopt");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let base = tempfile::tempdir().expect("create temp base");
        let hooks = base.path().join("hooks");
        let overlay_root = base.path().join("overlay");
        std::fs::create_dir_all(&overlay_root).expect("create overlay root");
        let rel = "src/lib.rs";
        let bytes = b"pub fn overlay_adopted() {}\n";
        write_file(&overlay_root, rel, bytes);
        init_repo(&overlay_root, &hooks);
        commit_all(&overlay_root, &hooks);
        let content_hash = crate::index::hasher::content_hash(bytes);

        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        let overlay_project_id = unique_id("gcode-blob-overlay-adopt");
        let parent_project_id = unique_id("gcode-blob-overlay-adopt-parent");
        cleanup(&mut conn, &overlay_project_id).expect("pre-clean overlay");
        cleanup(&mut conn, &parent_project_id).expect("pre-clean parent");
        let _overlay_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: overlay_project_id.clone(),
        };
        let _parent_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: parent_project_id.clone(),
        };
        seed_overlay_parent(&mut conn, &machine_id, &parent_project_id, rel);
        // The overlay has this content version synced but no selector on this
        // machine, so the run adopts it instead of parsing.
        seed_project_stats(&mut conn, &machine_id, &overlay_project_id);
        let overlay_file = indexed_file_row(&overlay_project_id, rel, &content_hash);
        api::upsert_file(&mut conn, &overlay_file).expect("seed overlay content version");
        mark_content_synced(&mut conn, &overlay_project_id, rel, &content_hash);

        let ctx = selector_context(
            database_url,
            &overlay_root,
            &overlay_project_id,
            ProjectIndexScope::Overlay {
                overlay_project_id: overlay_project_id.clone(),
                overlay_root: overlay_root.clone(),
                parent_project_id: parent_project_id.clone(),
                parent_root: std::path::PathBuf::from(format!("/tmp/{parent_project_id}")),
            },
        );
        let outcome = index_files(
            selector_request(&overlay_root, false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("overlay adoption run");
        assert_eq!(
            outcome.indexed_files, 0,
            "overlay run must adopt, not parse"
        );
        assert_eq!(outcome.skipped_files, 1);
        assert_eq!(
            selector_git_blob(&mut conn, &overlay_project_id, rel),
            Some(head_blob(&overlay_root, &hooks, rel)),
            "the overlay adopt path back-fills the clean HEAD blob"
        );
    }

    const REPARSE_REL: &str = "src/reparse.rs";
    const ALL_SYNCED: ProjectionFlags = ProjectionFlags {
        vectors_synced: true,
        vector_attempted: true,
        graph_synced: true,
        graph_attempted: true,
    };
    const ALL_PENDING: ProjectionFlags = ProjectionFlags {
        vectors_synced: false,
        vector_attempted: false,
        graph_synced: false,
        graph_attempted: false,
    };

    #[derive(Debug, PartialEq)]
    struct ProjectionFlags {
        vectors_synced: bool,
        vector_attempted: bool,
        graph_synced: bool,
        graph_attempted: bool,
    }

    fn reparse_symbol(project_id: &str, name: &str, hash: &str, docstring: Option<&str>) -> Symbol {
        Symbol {
            id: Symbol::make_id(project_id, REPARSE_REL, hash, name, "function", 0),
            project_id: project_id.to_string(),
            file_path: REPARSE_REL.to_string(),
            name: name.to_string(),
            qualified_name: name.to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: name.len(),
            line_start: 1,
            line_end: 1,
            signature: Some(format!("fn {name}()")),
            docstring: docstring.map(str::to_string),
            parent_symbol_id: None,
            file_content_hash: hash.to_string(),
            content_hash: hash.to_string(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    fn reparse_facts(symbols: Vec<Symbol>, calls: Vec<CallRelation>) -> ParseResult {
        ParseResult {
            symbols,
            imports: Vec::new(),
            calls,
            inheritance: Vec::new(),
            source: b"fn body() {}\n".to_vec(),
        }
    }

    fn stamp_version_synced(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        let project_uuid = db::id_param(project_id).expect("project uuid");
        conn.execute(
            "UPDATE code_indexed_files
             SET vectors_synced = TRUE, vector_sync_attempted_at = NOW(),
                 graph_synced = TRUE, graph_sync_attempted_at = NOW()
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_uuid, &REPARSE_REL, &hash],
        )
        .expect("stamp version synced");
    }

    fn version_flags(conn: &mut postgres::Client, project_id: &str, hash: &str) -> ProjectionFlags {
        let project_uuid = db::id_param(project_id).expect("project uuid");
        let row = conn
            .query_one(
                "SELECT vectors_synced, vector_sync_attempted_at IS NOT NULL,
                        graph_synced, graph_sync_attempted_at IS NOT NULL
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
                &[&project_uuid, &REPARSE_REL, &hash],
            )
            .expect("load version flags");
        ProjectionFlags {
            vectors_synced: row.get(0),
            vector_attempted: row.get(1),
            graph_synced: row.get(2),
            graph_attempted: row.get(3),
        }
    }

    fn write_content_only(
        conn: &mut postgres::Client,
        project_id: &str,
        rel: &str,
        language: &str,
        hash: &str,
        source: &[u8],
    ) {
        let mut tx = conn.transaction().expect("tx");
        let mut sink = PostgresCodeFactSink::new(
            &mut tx,
            project_id,
            Path::new("/tmp/inherit"),
            api::IndexWriteMode::Overlay,
            &local_machine_id(),
        )
        .expect("sink");
        write_content_only_file_facts(
            &mut sink,
            project_id,
            rel,
            language,
            hash,
            source.len(),
            source,
        )
        .expect("write content-only facts");
        tx.commit().expect("commit");
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn content_only_reroute_drops_graph_rows_and_dirties_graph_only() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-content-only-reroute");
        let caller = reparse_symbol(&project_id, "caller", "hash-1", None);
        let call = CallRelation::new(
            caller.id.clone(),
            "helper".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![caller], vec![call]),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        write_content_only(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            b"fn caller() {}\n",
        );

        assert_eq!(
            version_flags(&mut conn, &project_id, "hash-1"),
            ProjectionFlags {
                vectors_synced: true,
                vector_attempted: true,
                graph_synced: false,
                graph_attempted: false,
            }
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn reparse_with_changed_symbol_set_dirties_both_flags() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-symbol-set");
        let old = reparse_symbol(&project_id, "old_name", "hash-1", None);
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![old], Vec::new()),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        let new = reparse_symbol(&project_id, "new_name", "hash-1", None);
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![new], Vec::new()),
        );

        assert_eq!(version_flags(&mut conn, &project_id, "hash-1"), ALL_PENDING);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn reparse_with_identical_facts_keeps_both_flags() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-identical");
        let stable = reparse_symbol(&project_id, "stable", "hash-1", None);
        let call = CallRelation::new(
            stable.id.clone(),
            "helper".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![stable.clone()], vec![call.clone()]),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![stable], vec![call]),
        );

        assert_eq!(version_flags(&mut conn, &project_id, "hash-1"), ALL_SYNCED);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn empty_reparse_of_empty_version_keeps_flags() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-empty");
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(Vec::new(), Vec::new()),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(Vec::new(), Vec::new()),
        );

        assert_eq!(version_flags(&mut conn, &project_id, "hash-1"), ALL_SYNCED);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn reparse_that_drops_all_facts_dirties_both_flags() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-drops-all");
        let stable = reparse_symbol(&project_id, "stable", "hash-1", None);
        let call = CallRelation::new(
            stable.id.clone(),
            "helper".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![stable], vec![call]),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(Vec::new(), Vec::new()),
        );

        assert_eq!(version_flags(&mut conn, &project_id, "hash-1"), ALL_PENDING);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn reparse_with_changed_docstring_dirties_vectors_only() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-docstring");
        let undocumented = reparse_symbol(&project_id, "documented", "hash-1", None);
        let call = CallRelation::new(
            undocumented.id.clone(),
            "helper".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![undocumented], vec![call.clone()]),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        let documented =
            reparse_symbol(&project_id, "documented", "hash-1", Some("now documented"));
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![documented], vec![call]),
        );

        assert_eq!(
            version_flags(&mut conn, &project_id, "hash-1"),
            ProjectionFlags {
                vectors_synced: false,
                vector_attempted: false,
                graph_synced: true,
                graph_attempted: true,
            }
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn reparse_with_changed_calls_dirties_graph_only() {
        let (mut conn, project_id, _cleanup) = seeded_project("gcode-reparse-calls");
        let caller = reparse_symbol(&project_id, "caller", "hash-1", None);
        let first = CallRelation::new(
            caller.id.clone(),
            "first_callee".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![caller.clone()], vec![first]),
        );
        stamp_version_synced(&mut conn, &project_id, "hash-1");

        let second = CallRelation::new(
            caller.id.clone(),
            "second_callee".to_string(),
            REPARSE_REL.to_string(),
            2,
        );
        write_facts(
            &mut conn,
            &project_id,
            REPARSE_REL,
            "rust",
            "hash-1",
            reparse_facts(vec![caller], vec![second]),
        );

        assert_eq!(
            version_flags(&mut conn, &project_id, "hash-1"),
            ProjectionFlags {
                vectors_synced: true,
                vector_attempted: true,
                graph_synced: false,
                graph_attempted: false,
            }
        );
    }
}
