use std::collections::{BTreeMap, HashSet};
use std::io::ErrorKind;
use std::path::Path;

use crate::commands::{scope, token_budget};
use crate::config::Context;
use crate::db;
use crate::index::languages;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;
use crate::search::fts;
use crate::utils::short_id;
use crate::visibility;

mod batch;
pub use batch::symbols;
#[cfg(test)]
use batch::{
    RetrievedSymbol, missing_symbols_hint, prepare_symbol_batch, render_symbol_batch_json,
    render_symbols_text,
};

pub fn outline(
    ctx: &Context,
    file: &str,
    limit: Option<usize>,
    offset: usize,
    token_budget: Option<usize>,
    format: Format,
    verbose: bool,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let file = file.to_string();
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

    let groups = outline_groups(symbols, verbose)?;
    let (total, limit, groups) = token_budget::window(groups, offset, limit);
    let meta = token_budget::CollectionPageMeta {
        project_id: &ctx.project_id,
        total,
        offset,
        limit,
        hint: None,
    };
    let has_more = total > offset.saturating_add(groups.len());
    let page = token_budget::paginate_results(
        groups,
        offset,
        has_more,
        token_budget,
        |groups, next_offset, budget_exceeded| match format {
            Format::Json => token_budget::render_json_page(
                meta,
                &flatten_outline_json(groups),
                next_offset,
                budget_exceeded,
            ),
            Format::Text => {
                token_budget::render_text_page(&render_outline_groups(groups, verbose), next_offset)
            }
        },
    );

    match format {
        Format::Json => token_budget::print_json_page(
            meta,
            &flatten_outline_json(&page.results),
            page.next_offset,
            page.budget_exceeded,
        ),
        Format::Text => {
            let rendered = token_budget::render_text_page(
                &render_outline_groups(&page.results, verbose),
                page.next_offset,
            );
            if rendered.is_empty() {
                Ok(())
            } else {
                output::print_text(&rendered)
            }
        }
    }
}

struct OutlineGroup {
    symbols: Vec<Symbol>,
    json: Vec<serde_json::Value>,
}

fn outline_groups(symbols: Vec<Symbol>, verbose: bool) -> anyhow::Result<Vec<OutlineGroup>> {
    let parent_by_id = symbols
        .iter()
        .map(|symbol| (symbol.id.as_str(), symbol.parent_symbol_id.as_deref()))
        .collect::<BTreeMap<_, _>>();
    let mut root_by_id = BTreeMap::new();
    for symbol in &symbols {
        let mut current = symbol.id.as_str();
        let mut seen = HashSet::new();
        while seen.insert(current) {
            let Some(Some(parent)) = parent_by_id.get(current) else {
                break;
            };
            current = parent;
        }
        root_by_id.insert(symbol.id.clone(), current.to_string());
    }

    let mut root_order = Vec::new();
    let mut seen_roots = HashSet::new();
    let mut groups: BTreeMap<String, OutlineGroup> = BTreeMap::new();
    for symbol in symbols {
        let root = root_by_id
            .get(&symbol.id)
            .cloned()
            .unwrap_or_else(|| symbol.id.clone());
        if seen_roots.insert(root.clone()) {
            root_order.push(root.clone());
        }
        let json = if verbose {
            serde_json::to_value(&symbol)?
        } else {
            serde_json::to_value(symbol.to_outline())?
        };
        let group = groups.entry(root).or_insert_with(|| OutlineGroup {
            symbols: Vec::new(),
            json: Vec::new(),
        });
        group.symbols.push(symbol);
        group.json.push(json);
    }

    Ok(root_order
        .into_iter()
        .filter_map(|root| groups.remove(&root))
        .collect())
}

fn flatten_outline_json(groups: &[OutlineGroup]) -> Vec<serde_json::Value> {
    groups
        .iter()
        .flat_map(|group| group.json.iter().cloned())
        .collect()
}

fn render_outline_groups(groups: &[OutlineGroup], verbose: bool) -> String {
    groups
        .iter()
        .map(|group| render_outline_text(&group.symbols, verbose))
        .collect::<Vec<_>>()
        .join("\n")
}

fn render_outline_text(symbols: &[Symbol], verbose: bool) -> String {
    let parent_by_id = symbols
        .iter()
        .map(|symbol| (symbol.id.as_str(), symbol.parent_symbol_id.as_deref()))
        .collect::<BTreeMap<_, _>>();

    symbols
        .iter()
        .map(|s| {
            let indent = "  ".repeat(outline_depth(s, &parent_by_id));
            format!("{indent}{}", format_outline_text_line(s, verbose))
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

fn format_outline_text_line(symbol: &Symbol, verbose: bool) -> String {
    let mut line = format!(
        "{}:{}-{} [{}] {}",
        symbol.file_path, symbol.line_start, symbol.line_end, symbol.kind, symbol.qualified_name
    );
    if verbose {
        line.push_str(" id=");
        line.push_str(&symbol.id);
    }
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
            if let Some(source) = read_symbol_source(&ctx.project_root, &s)? {
                // Report savings: symbol bytes vs full file bytes
                if source.symbol_bytes > 0 && source.file_bytes > source.symbol_bytes {
                    let url = gobby_core::daemon_url::daemon_url();
                    savings::report_savings(
                        &url,
                        source.file_bytes,
                        source.symbol_bytes,
                        ctx.grant_ai.as_ref().map(|grant| &grant.bundle),
                    );
                }

                match format {
                    Format::Json => {
                        let mut result = serde_json::to_value(&s)?;
                        result["source"] = serde_json::Value::String(source.text);
                        output::print_json(&result)
                    }
                    Format::Text => {
                        println!("{}", source.text);
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

#[derive(Debug)]
pub(super) struct ReadSymbolSource {
    pub(super) text: String,
    pub(super) file_bytes: usize,
    pub(super) symbol_bytes: usize,
}

pub(super) fn read_symbol_source(
    root: &Path,
    symbol: &Symbol,
) -> anyhow::Result<Option<ReadSymbolSource>> {
    let source = match std::fs::read(root.join(&symbol.file_path)) {
        Ok(source) => source,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let end = symbol.byte_end.min(source.len());
    let start = symbol.byte_start.min(end);
    let bytes = &source[start..end];
    Ok(Some(ReadSymbolSource {
        text: String::from_utf8_lossy(bytes).to_string(),
        file_bytes: source.len(),
        symbol_bytes: bytes.len(),
    }))
}

pub fn kinds(
    ctx: &Context,
    limit: Option<usize>,
    offset: usize,
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let kinds = visibility::visible_kinds(&mut conn, ctx)?;
    let (total, limit, kinds) = token_budget::window(kinds, offset, limit);
    let meta = token_budget::CollectionPageMeta {
        project_id: &ctx.project_id,
        total,
        offset,
        limit,
        hint: None,
    };
    let page = token_budget::paginate(kinds, meta, token_budget, format, |rows| rows.join("\n"));
    token_budget::print_page(&page, meta, format, |rows| rows.join("\n"))
}

pub fn tree(
    ctx: &Context,
    paths: &[String],
    limit: Option<usize>,
    offset: usize,
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
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
    let files = filter_tree_files(files, paths)?;

    let groups = tree_groups(&files);
    let (total, limit, groups) = token_budget::window(groups, offset, limit);
    let meta = token_budget::CollectionPageMeta {
        project_id: &ctx.project_id,
        total,
        offset,
        limit,
        hint: None,
    };
    let has_more = total > offset.saturating_add(groups.len());
    let page = token_budget::paginate_results(
        groups,
        offset,
        has_more,
        token_budget,
        |groups, next_offset, budget_exceeded| match format {
            Format::Json => token_budget::render_json_page(
                meta,
                &flatten_tree_json(groups),
                next_offset,
                budget_exceeded,
            ),
            Format::Text => {
                token_budget::render_text_page(&render_tree_groups(groups), next_offset)
            }
        },
    );

    match format {
        Format::Json => token_budget::print_json_page(
            meta,
            &flatten_tree_json(&page.results),
            page.next_offset,
            page.budget_exceeded,
        ),
        Format::Text => {
            let rendered = token_budget::render_text_page(
                &render_tree_groups(&page.results),
                page.next_offset,
            );
            if rendered.is_empty() {
                output::print_text("No results.")
            } else {
                output::print_text(&rendered)
            }
        }
    }
}

fn filter_tree_files(
    files: Vec<serde_json::Value>,
    paths: &[String],
) -> anyhow::Result<Vec<serde_json::Value>> {
    let patterns = fts::compile_patterns(&fts::expand_paths(paths))?;
    if patterns.is_empty() {
        return Ok(files);
    }
    Ok(files
        .into_iter()
        .filter(|file| {
            file["file_path"]
                .as_str()
                .is_some_and(|path| patterns.iter().any(|pattern| pattern.matches(path)))
        })
        .collect())
}

/// Format file summary rows as a directory tree.
///
/// Paths are grouped by their directory (`dir`) and displayed by filename
/// (`basename`). Root-level files are grouped under `.`, a leading `/` is
/// stripped for root files, and entries render as
/// `  {basename} [{language}] ({symbol_count} symbols)`.
struct TreeGroup {
    directory: String,
    files: Vec<serde_json::Value>,
}

fn tree_groups(files: &[serde_json::Value]) -> Vec<TreeGroup> {
    let mut grouped: BTreeMap<String, Vec<serde_json::Value>> = BTreeMap::new();
    for file in files {
        let file_path = file["file_path"].as_str().unwrap_or("");
        let directory = file_path
            .rsplit_once('/')
            .map(|(directory, _)| if directory.is_empty() { "." } else { directory })
            .unwrap_or(".");
        grouped
            .entry(directory.to_string())
            .or_default()
            .push(file.clone());
    }
    grouped
        .into_iter()
        .map(|(directory, files)| TreeGroup { directory, files })
        .collect()
}

fn flatten_tree_json(groups: &[TreeGroup]) -> Vec<serde_json::Value> {
    groups
        .iter()
        .flat_map(|group| group.files.iter().cloned())
        .collect()
}

fn render_tree_groups(groups: &[TreeGroup]) -> String {
    let mut lines = Vec::new();
    for group in groups {
        lines.push(group.directory.clone());
        for file in &group.files {
            let file_path = file["file_path"].as_str().unwrap_or("");
            let language = file["language"].as_str().unwrap_or("");
            let symbol_count = file["symbol_count"].as_i64().unwrap_or(0);
            let basename = file_path
                .rsplit_once('/')
                .map(|(_, basename)| basename)
                .filter(|basename| !basename.is_empty())
                .unwrap_or(file_path.trim_start_matches('/'));
            lines.push(format!(
                "  {basename} [{language}] ({symbol_count} symbols)"
            ));
        }
    }
    lines.join("\n")
}

#[cfg(test)]
fn format_tree_text(files: &[serde_json::Value]) -> String {
    render_tree_groups(&tree_groups(files))
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
        let line = format_outline_text_line(&symbol(), true);

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

        let outline = render_outline_text(&[parent, child, grandchild], true);
        let lines = outline.lines().collect::<Vec<_>>();

        assert!(lines[0].starts_with("src/commands.rs:"));
        assert!(lines[1].starts_with("  src/commands.rs:"));
        assert!(lines[2].starts_with("    src/commands.rs:"));
    }

    #[test]
    fn outline_paging_keeps_each_root_subtree_complete() {
        let mut parent = symbol();
        parent.id = "parent".to_string();
        let mut child = symbol();
        child.id = "child".to_string();
        child.parent_symbol_id = Some(parent.id.clone());
        child.qualified_name = "outline::child".to_string();
        let mut other = symbol();
        other.id = "other".to_string();
        other.qualified_name = "other".repeat(50);

        let groups = outline_groups(vec![parent, child, other], false).expect("outline groups");
        let render = |rows: &[OutlineGroup], next_offset, _| {
            format!(
                "{}\nnext={next_offset:?}",
                render_outline_groups(rows, false)
            )
        };
        let budget = token_budget::estimate_tokens(&render(&groups[..1], Some(1), false));
        let page = token_budget::paginate_results(groups, 0, false, Some(budget), render);

        assert_eq!(page.results.len(), 1);
        assert_eq!(
            page.results[0]
                .symbols
                .iter()
                .map(|symbol| symbol.id.as_str())
                .collect::<Vec<_>>(),
            vec!["parent", "child"]
        );
        assert_eq!(page.next_offset, Some(1));
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

    #[test]
    fn tree_filters_files_directories_globs_and_multiple_paths_before_paging() {
        let files = vec![
            serde_json::json!({
                "file_path": "src/lib.rs",
                "language": "rust",
                "symbol_count": 3,
            }),
            serde_json::json!({
                "file_path": "src/commands/grep.rs",
                "language": "rust",
                "symbol_count": 7,
            }),
            serde_json::json!({
                "file_path": "tests/cli.rs",
                "language": "rust",
                "symbol_count": 2,
            }),
        ];

        let exact_and_glob = filter_tree_files(
            files.clone(),
            &["src/lib.rs".to_string(), "tests/*.rs".to_string()],
        )
        .expect("filter exact file and glob");
        assert_eq!(
            exact_and_glob
                .iter()
                .filter_map(|file| file["file_path"].as_str())
                .collect::<Vec<_>>(),
            vec!["src/lib.rs", "tests/cli.rs"]
        );

        let directory = filter_tree_files(files.clone(), &["src".to_string()])
            .expect("filter directory prefix");
        assert_eq!(directory.len(), 2);

        let paged = filter_tree_files(files, &["tests".to_string()]).expect("filter before paging");
        let groups = tree_groups(&paged);
        let (total, _, page) = token_budget::window(groups, 0, Some(1));
        assert_eq!(total, 1, "filtering must happen before directory paging");
        assert_eq!(page.len(), 1);
    }

    #[test]
    fn tree_filter_rejects_invalid_glob() {
        let error = filter_tree_files(Vec::new(), &["[".to_string()])
            .expect_err("invalid glob should fail");
        assert!(error.to_string().contains("invalid path glob `[`"));
    }

    #[test]
    fn tree_paging_keeps_directory_groups_complete() {
        let files = vec![
            serde_json::json!({
                "file_path": "src/a.rs",
                "language": "rust",
                "symbol_count": 1,
            }),
            serde_json::json!({
                "file_path": "src/b.rs",
                "language": "rust",
                "symbol_count": 2,
            }),
            serde_json::json!({
                "file_path": "tests/a_very_long_fixture_name.rs",
                "language": "rust",
                "symbol_count": 3,
            }),
        ];
        let groups = tree_groups(&files);
        let render = |rows: &[TreeGroup], next_offset, _| {
            format!("{}\nnext={next_offset:?}", render_tree_groups(rows))
        };
        let budget = token_budget::estimate_tokens(&render(&groups[..1], Some(1), false));
        let page = token_budget::paginate_results(groups, 0, false, Some(budget), render);

        assert_eq!(page.results.len(), 1);
        assert_eq!(page.results[0].directory, "src");
        assert_eq!(page.results[0].files.len(), 2);
        assert_eq!(page.next_offset, Some(1));
    }

    #[test]
    fn symbol_batch_preserves_request_order_source_and_all_missing_ids() {
        let root = tempfile::tempdir().expect("temp project root");
        let source_dir = root.path().join("src");
        std::fs::create_dir_all(&source_dir).expect("create source directory");
        std::fs::write(source_dir.join("commands.rs"), "alpha\nbeta\ngamma\n")
            .expect("write source file");

        let mut first = symbol();
        first.id = "first".to_string();
        first.byte_start = 0;
        first.byte_end = 5;
        let mut second = symbol();
        second.id = "second".to_string();
        second.byte_start = 6;
        second.byte_end = 10;

        let ids = ["second", "missing-a", "first", "missing-b", "second"].map(str::to_string);
        let batch = prepare_symbol_batch(root.path(), &ids, vec![first, second])
            .expect("prepare symbol batch");

        assert_eq!(
            batch
                .results
                .iter()
                .map(|result| result.symbol.id.as_str())
                .collect::<Vec<_>>(),
            vec!["second", "first", "second"]
        );
        assert_eq!(
            batch
                .results
                .iter()
                .map(|result| result.source.as_deref())
                .collect::<Vec<_>>(),
            vec![Some("beta"), Some("alpha"), Some("beta")]
        );
        assert_eq!(batch.missing_ids, vec!["missing-a", "missing-b"]);
    }

    #[test]
    fn symbol_source_clamps_stale_byte_ranges_to_current_file() {
        let root = tempfile::tempdir().expect("temp project root");
        let source_dir = root.path().join("src");
        std::fs::create_dir_all(&source_dir).expect("create source directory");
        std::fs::write(source_dir.join("commands.rs"), b"012345").expect("write source file");
        let mut stale = symbol();
        stale.byte_start = 3;
        stale.byte_end = usize::MAX;

        let source = read_symbol_source(root.path(), &stale)
            .expect("read symbol source")
            .expect("source file exists");

        assert_eq!(source.text, "345");
        assert_eq!(source.symbol_bytes, 3);
        assert_eq!(source.file_bytes, 6);
    }

    #[test]
    fn symbol_batch_text_renders_bounded_source_and_missing_id_recovery() {
        let result = RetrievedSymbol {
            symbol: symbol(),
            source: Some("fn example() {}".to_string()),
        };
        let missing = vec!["missing-a".to_string(), "missing-b".to_string()];

        let rendered = render_symbols_text(std::slice::from_ref(&result), &missing);

        assert!(rendered.contains("src/commands.rs:7-63 [function] outline"));
        assert!(rendered.contains("fn example() {}"));
        assert!(rendered.contains("missing-a, missing-b"));
        assert!(rendered.contains("Edited files invalidate content-derived symbol IDs"));
        assert!(rendered.contains("gcode outline <file> --verbose"));
        assert!(rendered.contains("gcode symbol-at <path:line>"));
    }

    #[test]
    fn symbol_batch_json_pagination_is_lossless_with_source_and_missing_ids() {
        let mut results = Vec::new();
        for id in ["first", "second", "third"] {
            let mut stored = symbol();
            stored.id = id.to_string();
            results.push(RetrievedSymbol {
                symbol: stored,
                source: Some(id.repeat(200)),
            });
        }
        let missing = vec!["stale-id".to_string()];
        let hint = missing_symbols_hint(&missing).expect("missing hint");
        let first_meta = token_budget::CollectionPageMeta {
            project_id: "current-project",
            total: results.len(),
            offset: 0,
            limit: results.len(),
            hint: None,
        };
        let first_render = |rows: &[RetrievedSymbol], next_offset, budget_exceeded| {
            render_symbol_batch_json(
                rows,
                &missing,
                first_meta,
                next_offset,
                budget_exceeded,
                Some(&hint),
            )
        };
        let budget = token_budget::estimate_tokens(&first_render(&results[..1], Some(1), false));

        let mut offset = 0usize;
        let mut seen = Vec::new();
        while offset < results.len() {
            let meta = token_budget::CollectionPageMeta {
                project_id: "current-project",
                total: results.len(),
                offset,
                limit: results.len() - offset,
                hint: None,
            };
            let render = |rows: &[RetrievedSymbol], next_offset, budget_exceeded| {
                render_symbol_batch_json(
                    rows,
                    &missing,
                    meta,
                    next_offset,
                    budget_exceeded,
                    Some(&hint),
                )
            };
            let page = token_budget::paginate_results(
                results.iter().skip(offset).cloned().collect(),
                offset,
                false,
                Some(budget),
                render,
            );
            let rendered = render(&page.results, page.next_offset, page.budget_exceeded);
            let value: serde_json::Value =
                serde_json::from_str(&rendered).expect("symbol batch JSON");
            assert_eq!(value["missing_ids"], serde_json::json!(["stale-id"]));
            assert!(value["results"][0]["source"].is_string());
            seen.extend(page.results.iter().map(|result| result.symbol.id.clone()));
            let Some(next_offset) = page.next_offset else {
                break;
            };
            assert!(next_offset > offset);
            offset = next_offset;
        }

        assert_eq!(seen, vec!["first", "second", "third"]);
    }
}
