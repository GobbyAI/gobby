use crate::config::Context;
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};

pub fn summary(ctx: &Context, symbol_id: &str, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let sym: Option<Symbol> = conn
        .query_row(
            "SELECT * FROM code_symbols WHERE id = ?1",
            rusqlite::params![symbol_id],
            Symbol::from_row,
        )
        .ok();

    match sym {
        Some(s) => {
            let result = serde_json::json!({
                "id": s.id,
                "name": s.qualified_name,
                "kind": s.kind,
                "summary": s.summary,
            });
            match format {
                Format::Json => output::print_json(&result),
                Format::Text => {
                    if let Some(ref summary) = s.summary {
                        println!("{summary}");
                    } else {
                        eprintln!("No summary available for {}", s.qualified_name);
                    }
                    Ok(())
                }
            }
        }
        None => {
            eprintln!("Symbol not found: {symbol_id}");
            std::process::exit(1);
        }
    }
}

pub fn repo_outline(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;

    // Group files by directory with symbol counts
    let mut stmt = conn.prepare(
        "SELECT file_path, language, symbol_count FROM code_indexed_files \
         WHERE project_id = ?1 ORDER BY file_path",
    )?;

    let files: Vec<serde_json::Value> = stmt
        .query_map(rusqlite::params![&ctx.project_id], |row| {
            let fp: String = row.get(0)?;
            let lang: String = row.get(1)?;
            let count: i64 = row.get(2)?;
            Ok(serde_json::json!({
                "file_path": fp,
                "language": lang,
                "symbol_count": count,
            }))
        })?
        .filter_map(|r| r.ok())
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
