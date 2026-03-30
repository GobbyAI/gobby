use crate::config::Context;
use crate::db;
use crate::index::indexer;
use crate::models::IndexedProject;
use crate::output::{self, Format};

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
                println!("Project: {}", s.id);
                println!("Root: {}", s.root_path);
                println!("Files: {}", s.total_files);
                println!("Symbols: {}", s.total_symbols);
                println!("Last indexed: {}", s.last_indexed_at);
                println!("Duration: {}ms", s.index_duration_ms);
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

/// List all indexed projects from both standalone and gobby DBs.
pub fn projects(format: Format) -> anyhow::Result<()> {
    let gobby_dir = dirs::home_dir()
        .ok_or_else(|| anyhow::anyhow!("cannot determine home directory"))?
        .join(".gobby");

    let db_paths = [
        gobby_dir.join("gobby-code-index.db"),
        gobby_dir.join("gobby-hub.db"),
    ];

    let mut seen_ids = std::collections::HashSet::new();
    let mut all_projects: Vec<IndexedProject> = Vec::new();

    for db_path in &db_paths {
        if !db_path.exists() {
            continue;
        }
        let conn = match db::open_readonly(db_path) {
            Ok(c) => c,
            Err(_) => continue,
        };

        // Check table exists
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
                all_projects.push(project);
            }
        }
    }

    match format {
        Format::Json => output::print_json(&all_projects),
        Format::Text => {
            if all_projects.is_empty() {
                eprintln!("No indexed projects. Run `gcode init` in a project directory.");
            } else {
                for p in &all_projects {
                    let name = std::path::Path::new(&p.root_path)
                        .file_name()
                        .map(|n| n.to_string_lossy().to_string())
                        .unwrap_or_else(|| p.id.clone());
                    println!(
                        "{} — {} ({} files, {} symbols)",
                        name, p.root_path, p.total_files, p.total_symbols
                    );
                }
            }
            Ok(())
        }
    }
}
