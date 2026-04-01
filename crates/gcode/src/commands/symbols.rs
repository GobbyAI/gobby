use crate::config::Context;
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;

pub fn outline(ctx: &Context, file: &str, format: Format, verbose: bool) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let mut stmt = conn.prepare(
        "SELECT * FROM code_symbols WHERE project_id = ?1 AND file_path = ?2 ORDER BY line_start",
    )?;
    let symbols: Vec<Symbol> = stmt
        .query_map(rusqlite::params![&ctx.project_id, file], Symbol::from_row)?
        .filter_map(|r| r.ok())
        .collect();

    // Record savings: outline bytes vs full file bytes
    let file_path = ctx.project_root.join(file);
    if let Ok(meta) = file_path.metadata() {
        let file_bytes = meta.len() as usize;
        let outline_bytes: usize = symbols
            .iter()
            .map(|s| {
                // Approximate outline size: name + kind + line numbers + signature
                s.qualified_name.len()
                    + s.kind.len()
                    + s.signature.as_ref().map_or(0, |sig| sig.len())
                    + 20 // line numbers, separators
            })
            .sum();
        if file_bytes > outline_bytes {
            savings::print_savings(&format!("outline {file}"), file_bytes, outline_bytes);
            let metadata = serde_json::json!({"file": file, "symbols": symbols.len()}).to_string();
            let _ = savings::record_savings(
                &conn,
                "code_index",
                file_bytes,
                outline_bytes,
                Some(&ctx.project_id),
                Some(&metadata),
            );
        }
    }

    match format {
        Format::Json => {
            if verbose {
                output::print_json(&symbols)
            } else {
                let slim: Vec<_> = symbols.iter().map(|s| s.to_outline()).collect();
                output::print_json(&slim)
            }
        }
        Format::Text => {
            for s in &symbols {
                let indent = if s.parent_symbol_id.is_some() {
                    "  "
                } else {
                    ""
                };
                println!(
                    "{indent}{}:{} [{}] {}",
                    s.file_path, s.line_start, s.kind, s.qualified_name
                );
            }
            Ok(())
        }
    }
}

pub fn symbol(ctx: &Context, id: &str, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let sym: Option<Symbol> = conn
        .query_row(
            "SELECT * FROM code_symbols WHERE id = ?1",
            rusqlite::params![id],
            Symbol::from_row,
        )
        .ok();

    match sym {
        Some(s) => {
            let file_path = ctx.project_root.join(&s.file_path);
            if file_path.exists() {
                let source = std::fs::read(&file_path)?;
                let file_bytes = source.len();
                let end = s.byte_end.min(source.len());
                let start = s.byte_start.min(end);
                let symbol_bytes = end - start;
                let snippet = String::from_utf8_lossy(&source[start..end]);

                // Record savings: symbol bytes vs full file bytes
                if file_bytes > symbol_bytes {
                    savings::print_savings(
                        &format!("symbol {}", s.qualified_name),
                        file_bytes,
                        symbol_bytes,
                    );
                    let metadata = serde_json::json!({
                        "symbol": s.qualified_name,
                        "file": s.file_path
                    })
                    .to_string();
                    let _ = savings::record_savings(
                        &conn,
                        "code_index",
                        file_bytes,
                        symbol_bytes,
                        Some(&ctx.project_id),
                        Some(&metadata),
                    );
                }

                match format {
                    Format::Json => {
                        let mut result = serde_json::to_value(&s)?;
                        result["source"] = serde_json::Value::String(snippet.to_string());
                        output::print_json(&result)
                    }
                    Format::Text => {
                        println!("{snippet}");
                        Ok(())
                    }
                }
            } else {
                match format {
                    Format::Json => output::print_json(&s),
                    Format::Text => {
                        println!("{}: file not found on disk", s.file_path);
                        Ok(())
                    }
                }
            }
        }
        None => anyhow::bail!("Symbol not found: {id}"),
    }
}

pub fn symbols(ctx: &Context, ids: &[String], format: Format) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let placeholders: Vec<String> = (1..=ids.len()).map(|i| format!("?{i}")).collect();
    let sql = format!(
        "SELECT * FROM code_symbols WHERE id IN ({})",
        placeholders.join(",")
    );
    let mut stmt = conn.prepare(&sql)?;
    let params: Vec<&dyn rusqlite::types::ToSql> = ids
        .iter()
        .map(|s| s as &dyn rusqlite::types::ToSql)
        .collect();
    let results: Vec<Symbol> = stmt
        .query_map(&*params, Symbol::from_row)?
        .filter_map(|r| r.ok())
        .collect();

    // Aggregate savings across batch
    let mut total_file_bytes = 0usize;
    let mut total_symbol_bytes = 0usize;
    for s in &results {
        let file_path = ctx.project_root.join(&s.file_path);
        if let Ok(meta) = file_path.metadata() {
            total_file_bytes += meta.len() as usize;
            total_symbol_bytes += s.byte_end - s.byte_start;
        }
    }
    if total_file_bytes > total_symbol_bytes {
        savings::print_savings(
            &format!("symbols ({})", results.len()),
            total_file_bytes,
            total_symbol_bytes,
        );
        let metadata = serde_json::json!({"count": results.len(), "ids": ids}).to_string();
        let _ = savings::record_savings(
            &conn,
            "code_index",
            total_file_bytes,
            total_symbol_bytes,
            Some(&ctx.project_id),
            Some(&metadata),
        );
    }

    match format {
        Format::Json => output::print_json(&results),
        Format::Text => {
            for s in &results {
                println!(
                    "{}:{} [{}] {}",
                    s.file_path, s.line_start, s.kind, s.qualified_name
                );
            }
            Ok(())
        }
    }
}

pub fn tree(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let mut stmt = conn.prepare(
        "SELECT file_path, language, symbol_count FROM code_indexed_files \
         WHERE project_id = ?1 ORDER BY file_path",
    )?;

    let files: Vec<serde_json::Value> = stmt
        .query_map(rusqlite::params![&ctx.project_id], |row| {
            Ok(serde_json::json!({
                "file_path": row.get::<_, String>(0)?,
                "language": row.get::<_, String>(1)?,
                "symbol_count": row.get::<_, i64>(2)?,
            }))
        })?
        .filter_map(|r| r.ok())
        .collect();

    match format {
        Format::Json => output::print_json(&files),
        Format::Text => {
            for f in &files {
                println!(
                    "{} [{}] ({} symbols)",
                    f["file_path"].as_str().unwrap_or(""),
                    f["language"].as_str().unwrap_or(""),
                    f["symbol_count"].as_i64().unwrap_or(0),
                );
            }
            Ok(())
        }
    }
}
