use std::path::PathBuf;

use crate::config::{Context, ProjectIndexScope};
use crate::index::indexer::{IndexOptions, IndexRequest, index_files, index_snapshot};

fn context(index_scope: ProjectIndexScope) -> Context {
    Context {
        // Rejections must happen before accessing Git or a database.
        database_url: "invalid-database-url".into(),
        project_root: PathBuf::from("/missing-snapshot-test-root"),
        project_id: "snapshot-test".into(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope,
    }
}

#[test]
fn snapshot_index_rejects_unsealed_or_mismatched_scope_before_io() {
    let commit = "a".repeat(40);
    let scopes = [
        ProjectIndexScope::Single,
        ProjectIndexScope::Overlay {
            overlay_project_id: "overlay-test".into(),
            overlay_root: PathBuf::from("/overlay"),
            parent_project_id: "parent-test".into(),
            parent_root: PathBuf::from("/parent"),
        },
        ProjectIndexScope::Snapshot {
            commit_oid: "b".repeat(40),
        },
    ];
    for scope in scopes {
        let error = index_snapshot(&context(scope), &commit).unwrap_err();
        assert!(
            error.to_string().contains("matching sealed snapshot scope"),
            "unexpected error: {error:#}"
        );
    }
}

#[test]
fn snapshot_index_rejects_every_incompatible_api_option_before_io() {
    let ctx = context(ProjectIndexScope::Snapshot {
        commit_oid: "a".repeat(40),
    });
    for option in ["path", "files", "full", "cpp", "sync"] {
        let mut request = IndexRequest {
            project_root: ctx.project_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        };
        match option {
            "path" => request.path_filter = Some("src".into()),
            "files" => request.explicit_files.push("src/main.rs".into()),
            "full" => request.full = true,
            "cpp" => request.require_cpp_semantics = true,
            "sync" => request.sync_projections = true,
            _ => unreachable!(),
        }
        let error = index_files(request, &ctx, IndexOptions::default()).unwrap_err();
        assert!(
            error.to_string().contains("complete captured inventory"),
            "option {option}: unexpected error: {error:#}"
        );
    }
}
