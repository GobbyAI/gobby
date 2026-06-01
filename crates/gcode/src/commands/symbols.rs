use std::collections::BTreeMap;

use gobby_core::ai::{daemon::generate_via_daemon, effective_route, text::generate_text};
use gobby_core::ai_context::{AiConfigSource, AiContext, PostgresAiConfigSource};
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::scope;
use crate::config::{self, Context};
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;
use crate::secrets;
use crate::utils::short_id;
use crate::visibility;

const OUTLINE_SYSTEM_PROMPT: &str = "You write concise code outlines for developers. Return a compact natural-language outline focused on responsibilities, main symbols, and notable control flow. Do not include markdown fences.";

pub fn outline(
    ctx: &Context,
    file: &str,
    format: Format,
    verbose: bool,
    summarize: bool,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let file = scope::normalize_file_arg(ctx, file);
    let symbols = visibility::visible_symbols_for_file(&mut conn, ctx, &file)?;

    if symbols.is_empty() && !ctx.quiet {
        eprintln!("{}", outline_missing_diagnostic(&mut conn, ctx, &file));
    }

    if summarize && let Some(summary) = summarize_outline(ctx, &file, &symbols) {
        return output::print_text(&summary);
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
            let outline = render_outline_text(&symbols);
            if outline.is_empty() {
                Ok(())
            } else {
                output::print_text(&outline)
            }
        }
    }
}

fn summarize_outline(ctx: &Context, file: &str, symbols: &[Symbol]) -> Option<String> {
    let code = std::fs::read_to_string(ctx.project_root.join(file)).ok()?;
    let ai_context = resolve_outline_ai_context(ctx).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);

    summarize_outline_with(file, &code, symbols, |prompt, system| {
        let result = match route {
            AiRouting::Daemon => generate_via_daemon(&ai_context, prompt, Some(system)),
            AiRouting::Direct => generate_text(&ai_context, prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => return None,
        };
        result.ok().map(|result| result.text)
    })
}

fn resolve_outline_ai_context(ctx: &Context) -> anyhow::Result<AiContext> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    Ok(AiContext::resolve(
        Some(ctx.project_id.clone()),
        &mut source,
    ))
}

fn summarize_outline_with(
    file: &str,
    code: &str,
    symbols: &[Symbol],
    generate: impl FnOnce(&str, &str) -> Option<String>,
) -> Option<String> {
    if code.trim().is_empty() {
        return None;
    }
    let prompt = outline_summary_prompt(file, code, symbols);
    generate(&prompt, OUTLINE_SYSTEM_PROMPT).and_then(|summary| {
        let summary = summary.trim();
        (!summary.is_empty()).then(|| summary.to_string())
    })
}

fn outline_summary_prompt(file: &str, code: &str, symbols: &[Symbol]) -> String {
    let mut prompt = format!("File: {file}\n\nSymbols:\n");
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
    } else {
        for symbol in symbols {
            prompt.push_str(&format!(
                "- {} [{}] lines {}-{}",
                symbol.qualified_name, symbol.kind, symbol.line_start, symbol.line_end
            ));
            if let Some(signature) = symbol
                .signature
                .as_deref()
                .filter(|value| !value.is_empty())
            {
                prompt.push_str(&format!(": {signature}"));
            }
            prompt.push('\n');
        }
    }
    prompt.push_str("\nCode:\n");
    prompt.push_str(code);
    prompt
}

fn render_outline_text(symbols: &[Symbol]) -> String {
    symbols
        .iter()
        .map(|s| {
            let indent = if s.parent_symbol_id.is_some() {
                "  "
            } else {
                ""
            };
            format!("{indent}{}", format_outline_text_line(s))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn outline_missing_diagnostic(conn: &mut postgres::Client, ctx: &Context, file: &str) -> String {
    if scope::path_exists_in_current_project(ctx, file) {
        if visibility::indexed_file_exists(conn, ctx, file) {
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
    fn summarizes_when_configured() {
        let symbols = vec![symbol()];
        let summary = summarize_outline_with(
            "src/commands.rs",
            "pub fn outline() -> anyhow::Result<()> { Ok(()) }",
            &symbols,
            |prompt, system| {
                assert_eq!(system, OUTLINE_SYSTEM_PROMPT);
                assert!(prompt.contains("File: src/commands.rs"));
                assert!(prompt.contains("Symbols:"));
                assert!(prompt.contains("outline [function] lines 7-63"));
                assert!(prompt.contains("Code:"));
                assert!(prompt.contains("pub fn outline()"));
                Some("Natural-language outline".to_string())
            },
        );

        assert_eq!(summary, Some("Natural-language outline".to_string()));
    }

    #[test]
    fn degrades_to_ast() {
        let symbols = vec![symbol()];
        let ast_outline = render_outline_text(&symbols);
        let output = summarize_outline_with(
            "src/commands.rs",
            "pub fn outline() -> anyhow::Result<()> { Ok(()) }",
            &symbols,
            |_prompt, _system| None,
        )
        .unwrap_or(ast_outline.clone());

        assert_eq!(output, ast_outline);
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
