use std::path::Path;

use crate::config;
use crate::db;
use crate::index::indexer;
use crate::output::{self, Format};
use crate::project;
use crate::skill;

pub fn run(project_root: &Path, format: Format, quiet: bool) -> anyhow::Result<()> {
    let (project_id, was_created) = project::ensure_gcode_json(project_root)?;

    let status = if was_created {
        "initialized"
    } else if project_root.join(".gobby").join("project.json").exists() {
        "gobby"
    } else {
        "existing"
    };

    // Detect AI CLIs and install skills (skip if gobby manages this project)
    let mut installed_skills: Vec<String> = Vec::new();
    if status != "gobby" {
        let clis = skill::detect_clis(project_root);
        for cli in &clis {
            match skill::install_skill(project_root, cli) {
                Ok(path) if !path.is_empty() => {
                    if !quiet {
                        eprintln!("Installed gcode skill for {} → {}", cli.name, path);
                    }
                    installed_skills.push(cli.name.to_string());
                }
                Err(e) => {
                    if !quiet {
                        eprintln!("Warning: failed to install skill for {}: {}", cli.name, e);
                    }
                }
                _ => {}
            }
        }
    }

    // Auto-index the project (resolve DB path directly — Context::resolve() can't run yet)
    let db_path = config::resolve_db_path(project_root)?;
    let conn = db::open_readwrite(&db_path)?;
    let index_result = indexer::index_directory(
        &conn,
        project_root,
        &project_id,
        true,
        None,
        None,
        quiet,
        None,
    )?;
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
