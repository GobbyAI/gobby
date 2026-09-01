use std::path::Path;

use crate::config;
use crate::db;
use crate::index::api;
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
use crate::output::{self, Format};
use crate::skill;

pub fn run(project_root: &Path, format: Format, quiet: bool) -> anyhow::Result<()> {
    // Primary index writes are fenced on this machine's registered checkout,
    // so a root without a Gobby identity fails here (`checkout_required`)
    // instead of generating an identity Gobby would never register.
    let identity = config::resolve_project_identity(project_root)?;
    config::warn_project_identity(&identity, quiet);
    let project_id = identity.project_id.clone();

    let status = match identity.source {
        config::ProjectIdentitySource::IsolatedRoot => "isolated",
        config::ProjectIdentitySource::LinkedWorktree => "linked-worktree",
        config::ProjectIdentitySource::ProjectJson => "gobby",
        config::ProjectIdentitySource::IsolatedOverlay => "existing",
    };

    // Install AI CLI skills (skip if Gobby manages this project)
    let mut installed_skills: Vec<String> = Vec::new();
    if status != "gobby" {
        for target in skill::supported_targets() {
            match skill::install_skill(project_root, target) {
                Ok(path) if !path.is_empty() => {
                    if !quiet {
                        eprintln!(
                            "Installed gcode skill for {} → {}",
                            target.display_name, path
                        );
                    }
                    installed_skills.push(target.display_name.to_string());
                }
                Err(e) if !quiet => {
                    eprintln!(
                        "Warning: failed to install skill for {}: {}",
                        target.display_name, e
                    );
                }
                _ => {}
            }
        }
    }

    // Auto-index the project. The daemon process is not required, but a migrated
    // PostgreSQL hub must already be configured in Gobby bootstrap.
    let database_url = db::resolve_database_url()?;
    let index_ctx = config::Context {
        database_url,
        project_root: project_root.to_path_buf(),
        project_id: project_id.clone(),
        quiet,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: config::ProjectIndexScope::Single,
    };
    let index_result =
        match index_lock::with_project_lock(&index_ctx, IndexLockPolicy::wait(), || {
            api::index_files(
                api::IndexRequest {
                    project_root: project_root.to_path_buf(),
                    path_filter: None,
                    explicit_files: Vec::new(),
                    full: false,
                    require_cpp_semantics: false,
                    sync_projections: false,
                },
                &index_ctx,
                api::IndexOptions::default(),
            )
        })? {
            IndexLockResult::Acquired(outcome) => outcome,
            IndexLockResult::Busy(_) => {
                unreachable!("wait policy always acquires the index lock")
            }
        };
    if !quiet {
        eprintln!(
            "Indexed {} files, {} symbols in {}ms",
            index_result.indexed_files,
            index_result.symbols_indexed,
            index_result.durations.total_ms
        );
    }

    match format {
        Format::Json => {
            let mut result = serde_json::json!({
                "project_id": project_id,
                "project_root": project_root.to_string_lossy(),
                "status": status,
                "files_indexed": index_result.indexed_files,
                "symbols_found": index_result.symbols_indexed,
                "duration_ms": index_result.durations.total_ms,
            });
            if !installed_skills.is_empty() {
                result["skills_installed"] = serde_json::json!(installed_skills);
            }
            output::print_json(&result)
        }
        Format::Text => {
            if !quiet {
                match status {
                    "gobby" => {
                        eprintln!(
                            "Using gobby project: {} ({})",
                            project_id,
                            project_root.display()
                        );
                    }
                    "isolated" | "linked-worktree" => {
                        eprintln!(
                            "Using {} code index: {} ({})",
                            status,
                            project_id,
                            project_root.display()
                        );
                    }
                    _ => {
                        eprintln!(
                            "Already initialized: {} ({})",
                            project_id,
                            project_root.display()
                        );
                    }
                }
            }
            Ok(())
        }
    }
}
