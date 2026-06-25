use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use crate::config;
use crate::models::IndexedProject;
use crate::output::{self, Format};
use crate::utils::short_id;

use super::shared::{collect_projects, display_name, format_coverage, format_timestamp};

pub fn projects(format: Format) -> anyhow::Result<()> {
    let all_projects = collect_projects()?;

    match format {
        Format::Json => output::print_json(&all_projects),
        Format::Text => {
            if all_projects.is_empty() {
                eprintln!("No indexed projects. Run `gcode init` in a project directory.");
            } else {
                let mut text = String::new();
                for p in &all_projects {
                    text.push_str(&format!("{} — {}\n", display_name(p), p.root_path));
                    text.push_str(&format!(
                        "  {} files, {} symbols | Last indexed: {}\n",
                        format_coverage(p.total_files, p.total_eligible_files),
                        p.total_symbols,
                        format_timestamp(&p.last_indexed_at)
                    ));
                }
                output::print_text(text.trim_end())?;
            }
            Ok(())
        }
    }
}

fn is_stale(p: &IndexedProject) -> Option<&'static str> {
    if p.id.starts_with("00000000") {
        return Some("sentinel project (not a code project)");
    }
    if p.root_path.is_empty() {
        return Some("empty root path");
    }
    if !Path::new(&p.root_path).is_absolute() {
        return Some("relative root path");
    }
    if !Path::new(&p.root_path).exists() {
        return Some("path does not exist");
    }
    None
}

pub(super) struct StaleProject<'a> {
    pub(super) project: &'a IndexedProject,
    pub(super) reason: String,
}

pub(super) fn stale_projects(projects: &[IndexedProject]) -> Vec<StaleProject<'_>> {
    let mut stale = Vec::new();
    let mut stale_ids = HashSet::new();

    for project in projects {
        if let Some(reason) = is_stale(project) {
            stale_ids.insert(project.id.clone());
            stale.push(StaleProject {
                project,
                reason: reason.to_string(),
            });
        }
    }

    let mut by_root: BTreeMap<PathBuf, Vec<&IndexedProject>> = BTreeMap::new();
    for project in projects {
        if stale_ids.contains(&project.id) {
            continue;
        }
        let Ok(canonical_root) = Path::new(&project.root_path).canonicalize() else {
            continue;
        };
        by_root.entry(canonical_root).or_default().push(project);
    }

    for (root, entries) in by_root {
        if entries.len() < 2 {
            continue;
        }
        let Ok(identity) = config::resolve_project_identity(&root, config::MissingIdentity::Error)
        else {
            continue;
        };
        if !entries
            .iter()
            .any(|project| project.id == identity.project_id)
        {
            continue;
        }
        for project in entries {
            if project.id == identity.project_id || !stale_ids.insert(project.id.clone()) {
                continue;
            }
            stale.push(StaleProject {
                project,
                reason: format!(
                    "duplicate root superseded by current project id {}",
                    short_id(&identity.project_id)
                ),
            });
        }
    }

    stale
}

#[cfg(test)]
mod tests {
    use super::*;

    fn indexed_project(id: &str, root_path: &Path) -> IndexedProject {
        IndexedProject {
            id: id.to_string(),
            root_path: root_path.to_string_lossy().to_string(),
            total_files: 1,
            total_symbols: 1,
            last_indexed_at: "1".to_string(),
            index_duration_ms: 1,
            total_eligible_files: Some(1),
        }
    }

    fn write_project_json(root: &Path, id: &str) {
        let gobby_dir = root.join(".gobby");
        std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::json!({
                "id": id,
                "name": "project",
                "parent_project_path": root.to_string_lossy(),
                "parent_project_id": id
            })
            .to_string(),
        )
        .expect("write project.json");
    }

    #[test]
    fn duplicate_root_prune_detection_keeps_resolved_project_id() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().canonicalize().expect("canonical root");
        let current_id = "d45545c5-current-project-id";
        let stale_id = "39c31b8f-stale-project-id";
        write_project_json(&root, current_id);

        let projects = vec![
            indexed_project(current_id, &root),
            indexed_project(stale_id, &root),
        ];

        let stale = stale_projects(&projects);

        assert_eq!(stale.len(), 1);
        assert_eq!(stale[0].project.id, stale_id);
        assert!(stale[0].reason.contains("duplicate root"));
        assert!(stale.iter().all(|entry| entry.project.id != current_id));
    }
}
