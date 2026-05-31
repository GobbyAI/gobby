use super::file::{ExplicitFileRoute, explicit_file_route, write_parsed_file_facts};
use super::lifecycle::cleanup_deleted_file_projections;
use super::overlay::{
    IndexedFileState, OverlayReconcileAction, overlay_reconcile_action, valid_porcelain_status_byte,
};
use super::sink::CodeFactSink;
use super::util::DEFAULT_EXCLUDES;
use super::*;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, ParseResult, Symbol,
};
use crate::projection::sync::ProjectionTarget;
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

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();

    assert_eq!(
        explicit_file_route(root, &root.join("src/lib.rs"), &excludes),
        ExplicitFileRoute::Ast
    );
    assert_eq!(
        explicit_file_route(root, &root.join("notes.txt"), &excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("Dockerfile"), &excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("api_key.txt"), &excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("target/generated.txt"), &excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("image.bin"), &excludes),
        ExplicitFileRoute::Skip
    );
}

#[test]
fn explicit_file_route_skips_mjs_and_markdown() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/generated.mjs", b"export const value = 1;\n");
    write_file(root, "README.md", b"# Title\n");
    write_file(root, "docs/guide.markdown", b"# Guide\n");

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();

    assert_eq!(
        explicit_file_route(root, &root.join("src/generated.mjs"), &excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("README.md"), &excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("docs/guide.markdown"), &excludes),
        ExplicitFileRoute::Skip
    );
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
fn porcelain_status_byte_validation_matches_git_v1_codes() {
    for byte in [b' ', b'M', b'A', b'D', b'R', b'C', b'U', b'?', b'!'] {
        assert!(valid_porcelain_status_byte(byte));
    }
    for byte in [0, b'X', b'\n'] {
        assert!(!valid_porcelain_status_byte(byte));
    }
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
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_deleted_file_projections(&ctx, "src/deleted.rs", &mut outcome);

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
