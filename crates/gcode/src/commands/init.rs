use std::path::Path;

use crate::config;
use crate::db;
use crate::index::indexer;
use crate::output::{self, Format};
use crate::project;
use crate::skill;

pub fn run(project_root: &Path, format: Format, quiet: bool) -> anyhow::Result<()> {
    let identity =
        config::resolve_project_identity(project_root, config::MissingIdentity::Generate)?;
    config::warn_project_identity(&identity, quiet);
    let (project_id, was_created) = if identity.should_write_gcode_json {
        project::ensure_gcode_json(project_root)?
    } else {
        (identity.project_id.clone(), false)
    };

    let status = match identity.source {
        config::ProjectIdentitySource::IsolatedRoot => "isolated",
        config::ProjectIdentitySource::LinkedWorktree => "linked-worktree",
        config::ProjectIdentitySource::ProjectJson => "gobby",
        config::ProjectIdentitySource::Generated if was_created => "initialized",
        _ => "existing",
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
    let mut conn = db::connect_readwrite(&database_url)?;
    let index_result =
        indexer::index_directory(&mut conn, project_root, &project_id, true, quiet, false)?;
    if !quiet {
        eprintln!(
            "Indexed {} files, {} symbols in {}ms",
            index_result.files_indexed, index_result.symbols_found, index_result.duration_ms
        );
    }

    match format {
        Format::Json => {
            let mut result = serde_json::json!({
                "project_id": project_id,
                "project_root": project_root.to_string_lossy(),
                "status": status,
                "files_indexed": index_result.files_indexed,
                "symbols_found": index_result.symbols_found,
                "duration_ms": index_result.duration_ms,
            });
            if !installed_skills.is_empty() {
                result["skills_installed"] = serde_json::json!(installed_skills);
            }
            output::print_json(&result)
        }
        Format::Text => {
            if !quiet {
                match status {
                    "initialized" => {
                        eprintln!(
                            "Initialized project at {}\nProject ID: {}",
                            project_root.display(),
                            project_id
                        );
                    }
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
