use std::collections::BTreeMap;

use crate::commands::scope;
use crate::config::Context;
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;
use crate::utils::short_id;

pub fn outline(ctx: &Context, file: &str, format: Format, verbose: bool) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let file = scope::normalize_file_arg(ctx, file);
    let columns = db::symbol_select_columns("");
    let symbols: Vec<Symbol> = conn
        .query(
            &format!(
                "SELECT {columns} FROM code_symbols
                 WHERE project_id = $1 AND file_path = $2
                 ORDER BY line_start"
            ),
            &[&ctx.project_id, &file],
        )?
        .iter()
        .filter_map(|row| Symbol::from_row(row).ok())
        .collect();

    if symbols.is_empty() && !ctx.quiet {
        eprintln!("{}", outline_missing_diagnostic(&mut conn, ctx, &file));
    }

    // Report savings: outline bytes vs full file bytes
    let file_path = ctx.project_root.join(&file);
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
        if outline_bytes > 0
            && file_bytes > outline_bytes
            && let Some(url) = savings::resolve_daemon_url(None)
        {
            savings::report_savings(&url, file_bytes, outline_bytes);
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
                println!("{indent}{}", format_outline_text_line(s));
            }
            Ok(())
        }
    }
}

fn outline_missing_diagnostic(conn: &mut postgres::Client, ctx: &Context, file: &str) -> String {
    if scope::path_exists_in_current_project(ctx, file) {
        if scope::indexed_file_exists(conn, &ctx.project_id, file) {
            return format!("file has no indexed symbols in current project: {file}");
        }
        return format!("file not indexed in current project: {file}");
    }

    if let Some(owner) = scope::other_project_for_path(conn, ctx, file) {
        return format!(
            "path belongs to indexed project {} ({}); use --project {}",
            owner.root_path,
            short_id(&owner.id),
            owner.root_path
        );
    }

    if scope::indexed_file_exists(conn, &ctx.project_id, file)
        || scope::content_chunks_exist(conn, &ctx.project_id, file)
    {
        return format!("indexed path missing from current checkout: {file}; run gcode index");
    }

    format!("file not indexed in current project: {file}")
}

fn format_outline_text_line(symbol: &Symbol) -> String {
    let mut line = format!(
        "{}:{}-{} [{}] {} id={}",
        symbol.file_path,
        symbol.line_start,
        symbol.line_end,
        symbol.kind,
        symbol.qualified_name,
        symbol.id
    );
    if let Some(sig) = symbol.signature.as_deref().filter(|sig| !sig.is_empty()) {
        line.push_str(" sig=");
        line.push_str(sig);
    }
    line
}

pub fn symbol(ctx: &Context, id: &str, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let columns = db::symbol_select_columns("");
    let sym: Option<Symbol> = conn
        .query_opt(
            &format!("SELECT {columns} FROM code_symbols WHERE id = $1 AND project_id = $2"),
            &[&id, &ctx.project_id],
        )
        .ok()
        .flatten()
        .and_then(|row| Symbol::from_row(&row).ok());

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

                // Report savings: symbol bytes vs full file bytes
                if symbol_bytes > 0
                    && file_bytes > symbol_bytes
                    && let Some(url) = savings::resolve_daemon_url(None)
                {
                    savings::report_savings(&url, file_bytes, symbol_bytes);
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
        None => anyhow::bail!("Symbol not found in current project: {id}"),
    }
}

pub fn symbols(ctx: &Context, ids: &[String], format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    if ids.is_empty() {
        return match format {
            Format::Json => output::print_json(&Vec::<Symbol>::new()),
            Format::Text => Ok(()),
        };
    }
    let placeholders: Vec<String> = (1..=ids.len()).map(|i| format!("${i}")).collect();
    let project_placeholder = format!("${}", ids.len() + 1);
    let columns = db::symbol_select_columns("");
    let sql = format!(
        "SELECT {columns} FROM code_symbols
         WHERE id IN ({}) AND project_id = {project_placeholder}",
        placeholders.join(",")
    );
    let mut params: Vec<&(dyn postgres::types::ToSql + Sync)> = ids
        .iter()
        .map(|s| s as &(dyn postgres::types::ToSql + Sync))
        .collect();
    params.push(&ctx.project_id);
    let results: Vec<Symbol> = conn
        .query(&sql, &params)?
        .iter()
        .filter_map(|row| Symbol::from_row(row).ok())
        .collect();

    // Report aggregate savings across batch
    let mut total_file_bytes = 0usize;
    let mut total_symbol_bytes = 0usize;
    for s in &results {
        let file_path = ctx.project_root.join(&s.file_path);
        if let Ok(meta) = file_path.metadata() {
            total_file_bytes += meta.len() as usize;
            total_symbol_bytes += s.byte_end - s.byte_start;
        }
    }
    if total_symbol_bytes > 0
        && total_file_bytes > total_symbol_bytes
        && let Some(url) = savings::resolve_daemon_url(None)
    {
        savings::report_savings(&url, total_file_bytes, total_symbol_bytes);
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

pub fn kinds(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let kinds: Vec<String> = conn
        .query(
            "SELECT DISTINCT kind FROM code_symbols WHERE project_id = $1 ORDER BY kind",
            &[&ctx.project_id],
        )?
        .iter()
        .filter_map(|row| row.try_get(0).ok())
        .collect();

    match format {
        Format::Json => output::print_json(&kinds),
        Format::Text => {
            for k in &kinds {
                println!("{k}");
            }
            Ok(())
        }
    }
}

pub fn tree(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
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

    match format {
        Format::Json => output::print_json(&files),
        Format::Text => {
            let text = format_tree_text(&files);
            if text.is_empty() {
                Ok(())
            } else {
                output::print_text(&text)
            }
        }
    }
}

fn format_tree_text(files: &[serde_json::Value]) -> String {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();

    for file in files {
        let file_path = file["file_path"].as_str().unwrap_or("");
        let language = file["language"].as_str().unwrap_or("");
        let symbol_count = file["symbol_count"].as_i64().unwrap_or(0);
        let (dir, basename) = file_path
            .rsplit_once('/')
            .filter(|(dir, basename)| !dir.is_empty() && !basename.is_empty())
            .unwrap_or((".", file_path));

        groups.entry(dir.to_string()).or_default().push(format!(
            "  {basename} [{language}] ({symbol_count} symbols)"
        ));
    }

    let mut lines = Vec::new();
    for (dir, entries) in groups {
        lines.push(dir);
        lines.extend(entries);
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn symbol() -> Symbol {
        Symbol {
            id: "12345678-1234-5678-1234-567812345678".to_string(),
            project_id: "current-project".to_string(),
            file_path: "src/commands.rs".to_string(),
            name: "outline".to_string(),
            qualified_name: "outline".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 10,
            line_start: 7,
            line_end: 63,
            signature: Some("pub fn outline() -> anyhow::Result<()> {".to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn outline_text_line_includes_id_range_and_signature() {
        let line = format_outline_text_line(&symbol());

        assert!(line.contains("src/commands.rs:7-63 [function] outline"));
        assert!(line.contains("id=12345678-1234-5678-1234-567812345678"));
        assert!(line.contains("sig=pub fn outline() -> anyhow::Result<()> {"));
    }

    #[test]
    fn tree_text_groups_files_by_directory() {
        let files = vec![
            serde_json::json!({
                "file_path": "README.md",
                "language": "markdown",
                "symbol_count": 0,
            }),
            serde_json::json!({
                "file_path": "src/commands/grep.rs",
                "language": "rust",
                "symbol_count": 7,
            }),
            serde_json::json!({
                "file_path": "src/lib.rs",
                "language": "rust",
                "symbol_count": 3,
            }),
        ];

        assert_eq!(
            format_tree_text(&files),
            ".\n  README.md [markdown] (0 symbols)\nsrc\n  lib.rs [rust] (3 symbols)\nsrc/commands\n  grep.rs [rust] (7 symbols)"
        );
    }
}
