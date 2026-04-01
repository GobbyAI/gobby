use std::path::Path;

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
    let conn = db::open_readonly(&ctx.db_path)?;

    let stats: Option<IndexedProject> = conn
        .query_row(
            "SELECT * FROM code_indexed_projects WHERE id = ?1",
            rusqlite::params![&ctx.project_id],
            |row| {
                Ok(IndexedProject {
                    id: row.get("id")?,
                    root_path: row.get("root_path")?,
                    total_files: row.get::<_, i64>("total_files")? as usize,
                    total_symbols: row.get::<_, i64>("total_symbols")? as usize,
                    last_indexed_at: row
                        .get::<_, Option<String>>("last_indexed_at")?
                        .unwrap_or_default(),
                    index_duration_ms: row.get::<_, i64>("index_duration_ms")? as u64,
                })
            },
        )
        .ok();

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
                println!("  Files:    {}", s.total_files);
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

    let conn = db::open_readwrite(&ctx.db_path)?;
    indexer::invalidate(&conn, &ctx.project_id, ctx.daemon_url.as_deref())
}

/// Collect indexed projects from both standalone and gobby DBs.
/// Returns (project, db_path) pairs.
fn collect_projects() -> anyhow::Result<Vec<(IndexedProject, std::path::PathBuf)>> {
    let gobby_dir = dirs::home_dir()
        .ok_or_else(|| anyhow::anyhow!("cannot determine home directory"))?
        .join(".gobby");

    let db_paths = [
        gobby_dir.join("gobby-code-index.db"),
        gobby_dir.join("gobby-hub.db"),
    ];

    let mut seen_ids = std::collections::HashSet::new();
    let mut all: Vec<(IndexedProject, std::path::PathBuf)> = Vec::new();

    for db_path in &db_paths {
        if !db_path.exists() {
            continue;
        }
        let conn = match db::open_readonly(db_path) {
            Ok(c) => c,
            Err(_) => continue,
        };

        let has_table: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_indexed_projects')",
                [],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if !has_table {
            continue;
        }

        let mut stmt =
            conn.prepare("SELECT * FROM code_indexed_projects ORDER BY last_indexed_at DESC")?;
        let rows = stmt.query_map([], |row| {
            Ok(IndexedProject {
                id: row.get("id")?,
                root_path: row.get("root_path")?,
                total_files: row.get::<_, i64>("total_files")? as usize,
                total_symbols: row.get::<_, i64>("total_symbols")? as usize,
                last_indexed_at: row
                    .get::<_, Option<String>>("last_indexed_at")?
                    .unwrap_or_default(),
                index_duration_ms: row.get::<_, i64>("index_duration_ms")? as u64,
            })
        })?;

        for project in rows.flatten() {
            if seen_ids.insert(project.id.clone()) {
                all.push((project, db_path.clone()));
            }
        }
    }

    Ok(all)
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

/// List all indexed projects from both standalone and gobby DBs.
pub fn projects(format: Format) -> anyhow::Result<()> {
    let all_projects = collect_projects()?;

    match format {
        Format::Json => {
            let projects: Vec<&IndexedProject> = all_projects.iter().map(|(p, _)| p).collect();
            output::print_json(&projects)
        }
        Format::Text => {
            if all_projects.is_empty() {
                eprintln!("No indexed projects. Run `gcode init` in a project directory.");
            } else {
                for (p, _) in &all_projects {
                    println!("{} — {}", display_name(p), p.root_path);
                    println!(
                        "  {} files, {} symbols | Last indexed: {}",
                        p.total_files,
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

/// Remove stale project entries from the code index.
pub fn prune(force: bool) -> anyhow::Result<()> {
    let all_projects = collect_projects()?;
    let stale: Vec<_> = all_projects
        .iter()
        .filter_map(|(p, db_path)| is_stale(p).map(|reason| (p, db_path, reason)))
        .collect();

    if stale.is_empty() {
        eprintln!("No stale projects found.");
        return Ok(());
    }

    eprintln!("Found {} stale project(s):", stale.len());
    for (p, _, reason) in &stale {
        eprintln!("  {} — {}", display_name(p), reason);
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

    for (p, db_path, _) in &stale {
        let conn = db::open_readwrite(db_path)?;
        indexer::invalidate(&conn, &p.id, daemon_url.as_deref())?;
    }

    eprintln!("Pruned {} stale project(s).", stale.len());
    Ok(())
}
