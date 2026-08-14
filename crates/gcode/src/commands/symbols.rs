use std::collections::{BTreeMap, HashSet};

use crate::commands::scope;
use crate::config::Context;
use crate::db;
use crate::index::languages;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;
use crate::utils::short_id;
use crate::visibility;

pub fn outline(ctx: &Context, file: &str, format: Format, verbose: bool) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let file = scope::normalize_file_arg(ctx, file);
    let symbols = visibility::visible_symbols_for_file(&mut conn, ctx, &file)?;

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
        if outline_bytes > 0 && file_bytes > outline_bytes {
            let url = gobby_core::daemon_url::daemon_url();
            savings::report_savings(
                &url,
                file_bytes,
                outline_bytes,
                ctx.grant_ai.as_ref().map(|grant| &grant.bundle),
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
            let outline = render_outline_text(&symbols);
            if outline.is_empty() {
                Ok(())
            } else {
                output::print_text(&outline)
            }
        }
    }
}

fn render_outline_text(symbols: &[Symbol]) -> String {
    let parent_by_id = symbols
        .iter()
        .map(|symbol| (symbol.id.as_str(), symbol.parent_symbol_id.as_deref()))
        .collect::<BTreeMap<_, _>>();

    symbols
        .iter()
        .map(|s| {
            let indent = "  ".repeat(outline_depth(s, &parent_by_id));
            format!("{indent}{}", format_outline_text_line(s))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn outline_depth(symbol: &Symbol, parent_by_id: &BTreeMap<&str, Option<&str>>) -> usize {
    let mut depth = 0;
    let mut seen = HashSet::new();
    let mut current = symbol.parent_symbol_id.as_deref();
    while let Some(parent_id) = current {
        if !seen.insert(parent_id) {
            break;
        }
        let Some(parent_parent) = parent_by_id.get(parent_id) else {
            break;
        };
        depth += 1;
        current = *parent_parent;
    }
    depth
}

fn outline_missing_diagnostic(conn: &mut postgres::Client, ctx: &Context, file: &str) -> String {
    if scope::path_exists_in_current_project(ctx, file) {
        if visibility::indexed_file_exists(conn, ctx, file) {
            if let Some(message) = unsupported_file_type_diagnostic(file) {
                return message;
            }
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

    if visibility::indexed_file_exists(conn, ctx, file)
        || visibility::content_chunks_exist(conn, ctx, file)
    {
        return format!("indexed path missing from current checkout: {file}; run gcode index");
    }

    format!("file not indexed in current project: {file}")
}

fn unsupported_file_type_diagnostic(file: &str) -> Option<String> {
    if languages::detect_language(file).is_some() {
        return None;
    }

    Some(format!(
        "file type has no AST parser support; indexed as text chunks only: {file}"
    ))
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
    let sym = visibility::visible_symbol_by_id(&mut conn, ctx, id)?;

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
                if symbol_bytes > 0 && file_bytes > symbol_bytes {
                    let url = gobby_core::daemon_url::daemon_url();
                    savings::report_savings(
                        &url,
                        file_bytes,
                        symbol_bytes,
                        ctx.grant_ai.as_ref().map(|grant| &grant.bundle),
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
    let results = visibility::visible_symbols_by_ids(&mut conn, ctx, ids)?;

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
    if total_symbol_bytes > 0 && total_file_bytes > total_symbol_bytes {
        let url = gobby_core::daemon_url::daemon_url();
        savings::report_savings(
            &url,
            total_file_bytes,
            total_symbol_bytes,
            ctx.grant_ai.as_ref().map(|grant| &grant.bundle),
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

pub fn kinds(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let kinds = visibility::visible_kinds(&mut conn, ctx)?;

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
    let files: Vec<serde_json::Value> = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| {
            serde_json::json!({
                "file_path": file.file_path,
                "language": file.language,
                "symbol_count": file.symbol_count,
            })
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

/// Format file summary rows as a directory tree.
///
/// Paths are grouped by their directory (`dir`) and displayed by filename
/// (`basename`). Root-level files are grouped under `.`, a leading `/` is
/// stripped for root files, and entries render as
/// `  {basename} [{language}] ({symbol_count} symbols)`.
fn format_tree_text(files: &[serde_json::Value]) -> String {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();

    for file in files {
        let file_path = file["file_path"].as_str().unwrap_or("");
        let language = file["language"].as_str().unwrap_or("");
        let symbol_count = file["symbol_count"].as_i64().unwrap_or(0);
        let (dir, basename) = file_path
            .rsplit_once('/')
            .map(|(dir, basename)| {
                let dir = if dir.is_empty() { "." } else { dir };
                (dir, basename)
            })
            .filter(|(_, basename)| !basename.is_empty())
            .unwrap_or((".", file_path.trim_start_matches('/')));

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
            file_content_hash: String::new(),
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
    fn outline_text_indents_by_parent_chain_depth() {
        let mut parent = symbol();
        parent.id = "parent".to_string();
        parent.kind = "class".to_string();
        parent.qualified_name = "Parent".to_string();

        let mut child = symbol();
        child.id = "child".to_string();
        child.parent_symbol_id = Some(parent.id.clone());
        child.qualified_name = "Parent.child".to_string();

        let mut grandchild = symbol();
        grandchild.id = "grandchild".to_string();
        grandchild.parent_symbol_id = Some(child.id.clone());
        grandchild.qualified_name = "Parent.child.grandchild".to_string();

        let outline = render_outline_text(&[parent, child, grandchild]);
        let lines = outline.lines().collect::<Vec<_>>();

        assert!(lines[0].starts_with("src/commands.rs:"));
        assert!(lines[1].starts_with("  src/commands.rs:"));
        assert!(lines[2].starts_with("    src/commands.rs:"));
    }

    #[test]
    fn unsupported_file_type_diagnostic_mentions_text_only_indexing() {
        assert_eq!(
            unsupported_file_type_diagnostic("Dockerfile"),
            Some(
                "file type has no AST parser support; indexed as text chunks only: Dockerfile"
                    .to_string()
            )
        );
        assert_eq!(unsupported_file_type_diagnostic("src/lib.rs"), None);
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

    #[test]
    fn tree_text_treats_absolute_root_files_as_root_group() {
        let files = vec![serde_json::json!({
            "file_path": "/lib.rs",
            "language": "rust",
            "symbol_count": 1,
        })];

        assert_eq!(format_tree_text(&files), ".\n  lib.rs [rust] (1 symbols)");
    }
}
