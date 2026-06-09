use super::file::{ExplicitFileRoute, explicit_file_route, write_parsed_file_facts};
use super::lifecycle::{cleanup_deleted_file_projections, current_file_state};
use super::overlay::{IndexedFileState, OverlayReconcileAction, overlay_reconcile_action};
use super::pipeline::{
    cleanup_skipped_explicit_file_if_indexed, explicit_route_with_discovery_options,
};
use super::sink::CodeFactSink;
use super::util::DEFAULT_EXCLUDES;
use super::*;
use crate::config::Context;
use crate::index::walker;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, ParseResult, Symbol,
};
use crate::projection::sync::ProjectionTarget;
use crate::visibility;
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::path::Path;
use std::path::PathBuf;

fn write_file(root: &Path, rel: &str, contents: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(path, contents).expect("write file");
}

fn assert_cli_independent_contract<T>()
where
    T: Serialize + DeserializeOwned,
{
    let type_name = std::any::type_name::<T>();
    assert!(!type_name.contains("commands::"), "{type_name}");
    assert!(!type_name.contains("output::"), "{type_name}");
    assert!(!type_name.contains("clap"), "{type_name}");
}

#[test]
fn library_api_is_cli_independent() {
    assert_cli_independent_contract::<IndexRequest>();
    assert_cli_independent_contract::<IndexOutcome>();
    assert_cli_independent_contract::<IndexDurations>();
    assert_cli_independent_contract::<IndexDegradation>();

    let request = IndexRequest {
        project_root: PathBuf::from("/tmp/project"),
        path_filter: Some(PathBuf::from("src")),
        explicit_files: vec![PathBuf::from("src/lib.rs")],
        full: true,
        require_cpp_semantics: false,
        sync_projections: true,
    };

    let json = serde_json::to_value(&request).expect("request serializes");
    assert_eq!(json["project_root"], "/tmp/project");
    assert_eq!(json["path_filter"], "src");
    assert_eq!(json["explicit_files"][0], "src/lib.rs");
}

#[test]
fn invalidate_postgres_deletes_are_project_scoped() {
    let source = include_str!("lifecycle.rs");
    for expected in [
        "DELETE FROM code_symbols WHERE project_id = $1",
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        "DELETE FROM code_imports WHERE project_id = $1",
        "DELETE FROM code_calls WHERE project_id = $1",
        "DELETE FROM code_indexed_projects WHERE id = $1",
    ] {
        assert!(
            source.contains(expected),
            "missing scoped delete: {expected}"
        );
    }
    let truncate_code = ["TRUNCATE", " code_"].concat();
    let drop_table = ["DROP", " TABLE"].concat();
    assert!(!source.contains(&truncate_code));
    assert!(!source.contains(&drop_table));
}

#[test]
fn current_file_state_keeps_unhashable_paths_present() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "src/lib.rs", b"fn main() {}\n");
    std::fs::create_dir_all(temp.path().join("src/unreadable")).expect("create directory");

    let state = current_file_state(
        temp.path(),
        &[
            temp.path().join("src/lib.rs"),
            temp.path().join("src/unreadable"),
        ],
        &[],
    );

    assert!(state.present_paths.contains("src/lib.rs"));
    assert!(state.present_paths.contains("src/unreadable"));
    assert!(state.hashes.contains_key("src/lib.rs"));
    assert!(!state.hashes.contains_key("src/unreadable"));
}

#[test]
fn unsupported_file_types_group_content_only_paths() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "notes.txt", b"plain notes\n");
    write_file(temp.path(), "docs/tasks.TXT", b"more notes\n");
    write_file(temp.path(), "README.md", b"# Project\n");
    write_file(temp.path(), "docs/reference.markdown", b"# Reference\n");
    write_file(temp.path(), "Dockerfile", b"FROM rust:latest\n");

    let unsupported = super::util::unsupported_file_types(
        temp.path(),
        &[
            temp.path().join("notes.txt"),
            temp.path().join("docs/tasks.TXT"),
            temp.path().join("README.md"),
            temp.path().join("docs/reference.markdown"),
            temp.path().join("Dockerfile"),
        ],
    );

    assert_eq!(
        unsupported,
        vec![
            UnsupportedFileType {
                extension: ".markdown".to_string(),
                files: 1,
                examples: vec!["docs/reference.markdown".to_string()],
            },
            UnsupportedFileType {
                extension: ".md".to_string(),
                files: 1,
                examples: vec!["README.md".to_string()],
            },
            UnsupportedFileType {
                extension: ".txt".to_string(),
                files: 2,
                examples: vec!["notes.txt".to_string(), "docs/tasks.TXT".to_string()],
            },
            UnsupportedFileType {
                extension: "extensionless".to_string(),
                files: 1,
                examples: vec!["Dockerfile".to_string()],
            },
        ]
    );
}

#[derive(Default)]
struct RecordingCodeFactSink {
    writes: Vec<&'static str>,
    files: usize,
    symbols: usize,
    imports: usize,
    calls: usize,
    unresolved_targets: usize,
    chunks: usize,
}

impl CodeFactSink for RecordingCodeFactSink {
    fn delete_file_facts(&mut self, _project_id: &str, _file_path: &str) -> anyhow::Result<()> {
        self.writes.push("delete");
        Ok(())
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
    let caller_id = Symbol::make_id(project_id, rel, "caller", "function", 14);
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
        vec!["delete", "symbols", "file", "imports", "calls", "chunks"]
    );
    assert_eq!(sink.files, 1);
    assert_eq!(sink.symbols, 1);
    assert_eq!(sink.imports, 1);
    assert_eq!(sink.calls, 1);
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
fn call_relation_contract_uses_empty_optional_storage_values() {
    let resolved = CallRelation::new(
        "caller-1".to_string(),
        "foo".to_string(),
        "src/main.py".to_string(),
        12,
    )
    .with_symbol_target("callee-1".to_string());
    let unresolved = CallRelation::new(
        "caller-2".to_string(),
        "bar".to_string(),
        "src/main.py".to_string(),
        18,
    );

    assert_eq!(
        resolved.callee_symbol_id.as_deref().unwrap_or(""),
        "callee-1"
    );
    assert_eq!(unresolved.callee_symbol_id.as_deref().unwrap_or(""), "");
    assert_eq!(resolved.callee_target_kind, CallTargetKind::Symbol);
    assert_eq!(unresolved.callee_target_kind, CallTargetKind::Unresolved);
}

#[test]
fn explicit_file_route_sends_unsupported_text_to_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/lib.rs", b"fn main() {}\n");
    write_file(root, "notes.txt", b"plain notes\n");
    write_file(root, "Dockerfile", b"FROM rust:latest\n");
    write_file(root, "api_key.txt", b"secret-ish\n");
    write_file(root, "target/generated.txt", b"generated\n");
    write_file(root, "image.bin", b"PNG\0binary");

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/lib.rs"), excludes),
        ExplicitFileRoute::Ast
    );
    assert_eq!(
        explicit_file_route(root, &root.join("notes.txt"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("Dockerfile"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("api_key.txt"), excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("target/generated.txt"), excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("image.bin"), excludes),
        ExplicitFileRoute::Skip
    );
}

#[test]
fn explicit_file_routes_follow_respect_gitignore_setting() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, ".gitignore", b"ignored.rs\n");
    write_file(root, "ignored.rs", b"fn ignored() {}\n");
    write_file(root, "src/lib.rs", b"fn visible() {}\n");

    let route = explicit_route_with_discovery_options(
        root,
        &root.join("ignored.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: true,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Skip);
    let route = explicit_route_with_discovery_options(
        root,
        &root.join("src/lib.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: true,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Ast);

    let route = explicit_route_with_discovery_options(
        root,
        &root.join("ignored.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: false,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Ast);
}

#[test]
fn explicit_file_route_applies_parent_gitignore_without_full_discovery() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, "src/.gitignore", b"ignored.rs\n");
    write_file(root, "src/ignored.rs", b"fn ignored() {}\n");
    write_file(root, "src/visible.rs", b"fn visible() {}\n");

    assert_eq!(
        explicit_route_with_discovery_options(
            root,
            &root.join("src/ignored.rs"),
            DEFAULT_EXCLUDES,
            walker::DiscoveryOptions {
                respect_gitignore: true,
            },
        ),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_route_with_discovery_options(
            root,
            &root.join("src/visible.rs"),
            DEFAULT_EXCLUDES,
            walker::DiscoveryOptions {
                respect_gitignore: true,
            },
        ),
        ExplicitFileRoute::Ast
    );
}

#[test]
fn explicit_file_route_indexes_mjs_and_routes_markdown_to_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/module.mjs", b"export const value = 1;\n");
    write_file(root, "README.md", b"# Title\n");
    write_file(root, "docs/guide.markdown", b"# Guide\n");
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/module.mjs"), excludes),
        ExplicitFileRoute::Ast
    );
    assert_eq!(
        explicit_file_route(root, &root.join("README.md"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("docs/guide.markdown"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join(".github/workflows/ci.yml"), excludes),
        ExplicitFileRoute::ContentOnly
    );
}

#[test]
fn discovered_hidden_workflows_survive_index_path_filter() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");
    write_file(root, ".gobby/plans/plan.md", b"# Plan\n");

    let excludes = DEFAULT_EXCLUDES;
    let (_, content_only) = walker::discover_files(root, excludes);
    let filtered =
        super::util::filter_discovered_paths(root, Path::new(".github/workflows"), content_only);

    let mut rels: Vec<String> = filtered
        .into_iter()
        .map(|path| {
            path.strip_prefix(root)
                .expect("path under root")
                .to_string_lossy()
                .to_string()
        })
        .collect();
    rels.sort();
    assert_eq!(rels, vec![".github/workflows/ci.yml"]);
}

#[test]
fn explicit_file_route_skips_generated_mjs() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(
        root,
        "src/setup.mjs",
        b"// Generated by setup. Do not edit.\nexport const value = 1;\n",
    );

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/setup.mjs"), excludes),
        ExplicitFileRoute::Skip
    );
}

#[test]
fn explicit_skip_cleanup_deletes_stale_facts_and_projections() {
    let ctx = Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };
    let mut outcome = IndexOutcome::new("project-1");
    let mut deleted = false;

    cleanup_skipped_explicit_file_if_indexed(
        &ctx,
        "src/setup.mjs",
        &mut outcome,
        true,
        Some(true),
        || {
            deleted = true;
            Ok(())
        },
    )
    .expect("cleanup skipped explicit file");

    assert!(deleted);
    assert_eq!(outcome.skipped_files, 1);
    assert_eq!(outcome.degraded.len(), 2);
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Graph,
            ..
        } if file_path == "src/setup.mjs"
    )));
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Vectors,
            ..
        } if file_path == "src/setup.mjs"
    )));
}

#[test]
fn explicit_skip_cleanup_ignores_never_indexed_files() {
    let ctx = Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_skipped_explicit_file_if_indexed(
        &ctx,
        "src/secret.txt",
        &mut outcome,
        false,
        Some(false),
        || panic!("delete should not run for files without stale facts"),
    )
    .expect("cleanup skipped explicit file");

    assert_eq!(outcome.skipped_files, 1);
    assert!(outcome.degraded.is_empty());
}

#[test]
fn overlay_reconciliation_actions_cover_inherit_shadow_add_delete() {
    let parent = IndexedFileState {
        content_hash: "parent-hash".to_string(),
        language: "rust".to_string(),
    };
    let overlay = IndexedFileState {
        content_hash: "overlay-hash".to_string(),
        language: "rust".to_string(),
    };
    let tombstone = IndexedFileState {
        content_hash: visibility::TOMBSTONE_HASH.to_string(),
        language: visibility::TOMBSTONE_LANGUAGE.to_string(),
    };

    assert_eq!(
        overlay_reconcile_action(
            true,
            Some("parent-hash"),
            Some(&parent),
            Some(&overlay),
            true
        ),
        OverlayReconcileAction::Inherit
    );
    assert_eq!(
        overlay_reconcile_action(
            true,
            Some("edited-hash"),
            Some(&parent),
            Some(&overlay),
            true
        ),
        OverlayReconcileAction::Index
    );
    assert_eq!(
        overlay_reconcile_action(true, Some("added-hash"), None, None, true),
        OverlayReconcileAction::Index
    );
    assert_eq!(
        overlay_reconcile_action(false, None, Some(&parent), None, true),
        OverlayReconcileAction::Tombstone
    );
    assert_eq!(
        overlay_reconcile_action(false, None, Some(&parent), Some(&tombstone), true),
        OverlayReconcileAction::Skip
    );
    assert_eq!(
        overlay_reconcile_action(false, None, None, Some(&overlay), true),
        OverlayReconcileAction::DeleteOverlay
    );
}

#[test]
fn deleted_file_projection_cleanup_degrades_without_services() {
    let ctx = Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_deleted_file_projections(&ctx, "src/deleted.rs", &mut outcome, Some(true));

    assert_eq!(outcome.degraded.len(), 2);
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Graph,
            message,
        } if file_path == "src/deleted.rs"
            && message.contains("FalkorDB is not configured")
    )));
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Vectors,
            message,
        } if file_path == "src/deleted.rs"
            && message.contains("Qdrant config is required")
    )));
}

#[test]
fn deleted_file_projection_cleanup_skips_vectors_when_not_previously_synced() {
    let ctx = Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_deleted_file_projections(&ctx, "src/deleted.rs", &mut outcome, Some(false));

    assert_eq!(outcome.degraded.len(), 1);
    assert!(outcome.degraded.iter().all(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            target: ProjectionTarget::Graph,
            ..
        }
    )));
}
