use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use crate::config;
use crate::config::Context;
use crate::db;
use crate::index::indexer;
use crate::models::IndexedProject;
use crate::output::{self, Format};

/// Format a `last_indexed_at` value for display.
/// Handles both epoch seconds ("1774970556") and ISO 8601 ("2026-03-29T18:52:25.750230+00:00").
fn format_timestamp(raw: &str) -> String {
    if raw.is_empty() {
        return "never".to_string();
    }

    // Try epoch seconds first (all digits)
    if let Ok(epoch) = raw.parse::<i64>() {
        let secs = epoch % 60;
        let mins = (epoch / 60) % 60;
        let hours = (epoch / 3600) % 24;
        let days = epoch / 86400;

        // Simple date calculation from days since epoch
        let (year, month, day) = days_to_ymd(days);
        return format!("{year:04}-{month:02}-{day:02} {hours:02}:{mins:02}:{secs:02} UTC");
    }

    // Try ISO 8601 — extract the date/time portion before any fractional seconds or timezone
    if raw.len() >= 19 && raw.as_bytes().get(4) == Some(&b'-') {
        let base = &raw[..19]; // "2026-03-29T18:52:25"
        return base.replace('T', " ");
    }

    raw.to_string()
}

/// Convert days since Unix epoch to (year, month, day).
fn days_to_ymd(mut days: i64) -> (i64, i64, i64) {
    // Algorithm from http://howardhinnant.github.io/date_algorithms.html
    days += 719468;
    let era = if days >= 0 { days } else { days - 146096 } / 146097;
    let doe = days - era * 146097; // day of era [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // year of era [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // day of year [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // day [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // month [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

pub fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;

    let stats: Option<IndexedProject> = conn
        .query_opt(
            "SELECT id,
                    root_path,
                    total_files::BIGINT AS total_files,
                    total_symbols::BIGINT AS total_symbols,
                    last_indexed_at::TEXT AS last_indexed_at,
                    COALESCE(index_duration_ms, 0)::BIGINT AS index_duration_ms,
                    NULL::BIGINT AS total_eligible_files
             FROM code_indexed_projects WHERE id = $1",
            &[&ctx.project_id],
        )
        .ok()
        .flatten()
        .and_then(|row| indexed_project_from_row(&row).ok());

    match stats {
        Some(s) => match format {
            Format::Json => output::print_json(&s),
            Format::Text => {
                let name = Path::new(&s.root_path)
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| s.id.clone());
                println!("{} ({})", name, &s.id[..8]);
                println!("  Root:     {}", s.root_path);
                println!(
                    "  Files:    {}",
                    format_coverage(s.total_files, s.total_eligible_files)
                );
                println!("  Symbols:  {}", s.total_symbols);
                println!("  Indexed:  {}", format_timestamp(&s.last_indexed_at));
                println!("  Duration: {}ms", s.index_duration_ms);
                Ok(())
            }
        },
        None => {
            eprintln!(
                "No index found for project {}. Run `gcode index` first.",
                ctx.project_id
            );
            Ok(())
        }
    }
}

pub fn invalidate(ctx: &Context, force: bool) -> anyhow::Result<()> {
    if !force {
        let project_name = ctx
            .project_root
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| ctx.project_id.clone());

        eprint!(
            "This will clear the entire code index for '{}'. Continue? [y/N] ",
            project_name
        );
        let _ = std::io::Write::flush(&mut std::io::stderr());

        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            eprintln!("Aborted.");
            return Ok(());
        }
    }

    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    indexer::invalidate(&mut conn, &ctx.project_id, ctx.daemon_url.as_deref())
}

/// Collect indexed projects from the PostgreSQL hub.
fn collect_projects() -> anyhow::Result<Vec<IndexedProject>> {
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readonly(&database_url)?;
    let mut seen_ids = std::collections::HashSet::new();
    let mut all = Vec::new();
    let rows = conn.query(
        "SELECT id,
                root_path,
                total_files::BIGINT AS total_files,
                total_symbols::BIGINT AS total_symbols,
                last_indexed_at::TEXT AS last_indexed_at,
                COALESCE(index_duration_ms, 0)::BIGINT AS index_duration_ms,
                NULL::BIGINT AS total_eligible_files
         FROM code_indexed_projects
         ORDER BY last_indexed_at DESC NULLS LAST",
        &[],
    )?;

    for row in rows {
        if let Ok(project) = indexed_project_from_row(&row)
            && seen_ids.insert(project.id.clone())
        {
            all.push(project);
        }
    }

    Ok(all)
}

fn indexed_project_from_row(row: &postgres::Row) -> anyhow::Result<IndexedProject> {
    Ok(IndexedProject {
        id: row.try_get("id")?,
        root_path: row.try_get("root_path")?,
        total_files: row.try_get::<_, i64>("total_files")? as usize,
        total_symbols: row.try_get::<_, i64>("total_symbols")? as usize,
        last_indexed_at: row
            .try_get::<_, Option<String>>("last_indexed_at")?
            .unwrap_or_default(),
        index_duration_ms: row.try_get::<_, i64>("index_duration_ms")? as u64,
        total_eligible_files: row
            .try_get::<_, Option<i64>>("total_eligible_files")
            .ok()
            .flatten()
            .map(|n| n as usize),
    })
}

/// Format file count with optional coverage percentage.
fn format_coverage(indexed: usize, eligible: Option<usize>) -> String {
    match eligible {
        Some(total) if total > 0 => {
            let pct = (indexed as f64 / total as f64 * 100.0) as usize;
            format!("{indexed}/{total} ({pct}%)")
        }
        _ => format!("{indexed}"),
    }
}

/// Format a project name for display.
fn display_name(p: &IndexedProject) -> String {
    if p.root_path.is_empty() || !Path::new(&p.root_path).is_absolute() {
        return format!("<unknown> ({})", p.id);
    }
    let basename = Path::new(&p.root_path)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| p.id.clone());
    let short_id = if p.id.len() >= 8 { &p.id[..8] } else { &p.id };
    format!("{basename} ({short_id})")
}

/// List all indexed projects from the PostgreSQL hub.
pub fn projects(format: Format) -> anyhow::Result<()> {
    let all_projects = collect_projects()?;

    match format {
        Format::Json => output::print_json(&all_projects),
        Format::Text => {
            if all_projects.is_empty() {
                eprintln!("No indexed projects. Run `gcode init` in a project directory.");
            } else {
                for p in &all_projects {
                    println!("{} — {}", display_name(p), p.root_path);
                    println!(
                        "  {} files, {} symbols | Last indexed: {}",
                        format_coverage(p.total_files, p.total_eligible_files),
                        p.total_symbols,
                        format_timestamp(&p.last_indexed_at)
                    );
                }
            }
            Ok(())
        }
    }
}

/// Check if a project entry is stale.
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

#[derive(Debug)]
struct StaleProject<'a> {
    project: &'a IndexedProject,
    reason: String,
}

fn stale_projects(projects: &[IndexedProject]) -> Vec<StaleProject<'_>> {
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

fn short_id(id: &str) -> &str {
    id.get(..8).unwrap_or(id)
}

/// Remove stale project entries from the code index.
pub fn prune(force: bool) -> anyhow::Result<()> {
    let all_projects = collect_projects()?;
    let stale = stale_projects(&all_projects);

    if stale.is_empty() {
        eprintln!("No stale projects found.");
        return Ok(());
    }

    eprintln!("Found {} stale project(s):", stale.len());
    for stale_project in &stale {
        eprintln!(
            "  {} — {}",
            display_name(stale_project.project),
            stale_project.reason
        );
    }

    if !force {
        eprint!("\nRemove these entries and their indexed data? [y/N] ");
        let _ = std::io::Write::flush(&mut std::io::stderr());

        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            eprintln!("Aborted.");
            return Ok(());
        }
    }

    let daemon_url = config::resolve_daemon_url();
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readwrite(&database_url)?;

    for stale_project in &stale {
        indexer::invalidate(&mut conn, &stale_project.project.id, daemon_url.as_deref())?;
    }

    eprintln!("Pruned {} stale project(s).", stale.len());
    Ok(())
}

pub fn repo_outline(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;

    // Group files by directory with symbol counts.
    let files: Vec<serde_json::Value> = conn
        .query(
            "SELECT file_path, language, symbol_count::BIGINT AS symbol_count
             FROM code_indexed_files
             WHERE project_id = $1 ORDER BY file_path",
            &[&ctx.project_id],
        )?
        .iter()
        .filter_map(|row| {
            Some(serde_json::json!({
                "file_path": row.try_get::<_, String>("file_path").ok()?,
                "language": row.try_get::<_, String>("language").ok()?,
                "symbol_count": row.try_get::<_, i64>("symbol_count").ok()?,
            }))
        })
        .collect();

    // Group by directory
    let mut dirs: std::collections::BTreeMap<String, Vec<&serde_json::Value>> =
        std::collections::BTreeMap::new();
    for f in &files {
        let fp = f["file_path"].as_str().unwrap_or("");
        let dir = std::path::Path::new(fp)
            .parent()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| ".".to_string());
        dirs.entry(dir).or_default().push(f);
    }

    match format {
        Format::Json => output::print_json(&dirs),
        Format::Text => {
            for (dir, dir_files) in &dirs {
                let total_syms: i64 = dir_files
                    .iter()
                    .map(|f| f["symbol_count"].as_i64().unwrap_or(0))
                    .sum();
                println!("{dir}/ ({} files, {total_syms} symbols)", dir_files.len());
            }
            Ok(())
        }
    }
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
