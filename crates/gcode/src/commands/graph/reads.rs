use std::collections::BTreeMap;

use crate::config::Context;
use crate::db;
use crate::graph::code_graph;
use crate::models::{GraphResult, PagedResponse};
use crate::output::{self, Format};
use crate::search::fts::{self, ResolvedGraphSymbol};
use serde::Serialize;

const GOBBY_HINT: &str =
    "Graph commands require FalkorDB, available with Gobby. See: https://github.com/GobbyAI/gobby";

fn hint_for(ctx: &Context) -> Option<String> {
    if ctx.falkordb.is_none() {
        Some(GOBBY_HINT.to_string())
    } else {
        None
    }
}

fn print_graph_hint_text(ctx: &Context) {
    if ctx.falkordb.is_none() {
        eprintln!("Hint: {GOBBY_HINT}");
    }
}

fn graph_read_unavailable(error: &anyhow::Error) -> bool {
    matches!(
        error.downcast_ref::<code_graph::GraphReadError>(),
        Some(
            code_graph::GraphReadError::NotConfigured
                | code_graph::GraphReadError::Unreachable { .. }
        )
    )
}

fn empty_paged_response<T: Serialize>(
    ctx: &Context,
    offset: usize,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(&PagedResponse::<T> {
            project_id: ctx.project_id.clone(),
            total: 0,
            offset,
            limit,
            results: vec![],
            hint: hint_for(ctx),
        }),
        Format::Text => {
            print_graph_hint_text(ctx);
            Ok(())
        }
    }
}

pub(super) fn format_grouped_graph_results<F>(results: &[GraphResult], format_line: F) -> String
where
    F: Fn(&GraphResult) -> String,
{
    let mut grouped: BTreeMap<&str, Vec<&GraphResult>> = BTreeMap::new();
    for result in results {
        grouped.entry(&result.file_path).or_default().push(result);
    }

    let mut lines = Vec::new();
    for (file_path, mut entries) in grouped {
        lines.push(if file_path.is_empty() {
            "<unknown>".to_string()
        } else {
            file_path.to_string()
        });
        entries.sort_by(|a, b| {
            a.line
                .cmp(&b.line)
                .then_with(|| a.name.cmp(&b.name))
                .then_with(|| a.relation.cmp(&b.relation))
                .then_with(|| a.distance.cmp(&b.distance))
        });
        lines.extend(entries.into_iter().map(&format_line));
    }
    lines.join("\n")
}

/// Resolve user input to a canonical symbol id, printing suggestions on ambiguity.
/// Returns None and prints an error message if no match found.
fn resolve_symbol(ctx: &Context, input: &str) -> anyhow::Result<Option<ResolvedGraphSymbol>> {
    let mut conn = match db::connect_readonly(&ctx.database_url) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to open index for graph resolution: {e}");
            return Ok(None);
        }
    };
    let (resolved, suggestions) = fts::resolve_graph_symbol(&mut conn, input, &ctx.project_id)?;
    if resolved.is_none() {
        if suggestions.is_empty() {
            eprintln!("No symbol matching '{input}' found");
        } else {
            eprintln!(
                "Ambiguous symbol '{input}'. Refine the query. Matches: {}",
                suggestions.join(", ")
            );
        }
    }
    Ok(resolved)
}

fn resolve_symbol_or_empty_response(
    ctx: &Context,
    input: &str,
    offset: usize,
    limit: usize,
    format: Format,
) -> anyhow::Result<Option<ResolvedGraphSymbol>> {
    match resolve_symbol(ctx, input)? {
        Some(symbol) => Ok(Some(symbol)),
        None => {
            empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format)?;
            Ok(None)
        }
    }
}

pub fn callers(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    if let Err(err) = code_graph::require_graph_reads(ctx) {
        if graph_read_unavailable(&err) {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        return Err(err);
    }
    let Some(symbol) = resolve_symbol_or_empty_response(ctx, symbol_name, offset, limit, format)?
    else {
        return Ok(());
    };
    let total = match code_graph::count_callers(ctx, &symbol.id) {
        Ok(total) => total,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        Err(err) => return Err(err),
    };
    let results = match code_graph::find_callers(ctx, &symbol.id, offset, limit) {
        Ok(results) => results,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        Err(err) => return Err(err),
    };

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint: hint_for(ctx),
        }),
        Format::Text => {
            if results.is_empty() && offset == 0 {
                output::print_text(&format!("No callers found for '{}'", symbol.display_name))?;
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No callers at offset {offset} (total {total})");
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    format!("{} {} -> {}", r.line, r.name, symbol.display_name)
                }))?;
                if total > offset + results.len() {
                    eprintln!(
                        "-- {} of {} results (use --offset {} for more)",
                        results.len(),
                        total,
                        offset + results.len()
                    );
                }
            }
            Ok(())
        }
    }
}

pub fn usages(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    if let Err(err) = code_graph::require_graph_reads(ctx) {
        if graph_read_unavailable(&err) {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        return Err(err);
    }
    let Some(symbol) = resolve_symbol_or_empty_response(ctx, symbol_name, offset, limit, format)?
    else {
        return Ok(());
    };
    let total = match code_graph::count_usages(ctx, &symbol.id) {
        Ok(total) => total,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        Err(err) => return Err(err),
    };
    let results = match code_graph::find_usages(ctx, &symbol.id, offset, limit) {
        Ok(results) => results,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format);
        }
        Err(err) => return Err(err),
    };

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint: hint_for(ctx),
        }),
        Format::Text => {
            if results.is_empty() && offset == 0 {
                output::print_text(&format!("No usages found for '{}'", symbol.display_name))?;
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No usages at offset {offset} (total {total})");
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    let rel = r.relation.as_deref().unwrap_or("unknown");
                    format!("{} [{}] {} -> {}", r.line, rel, r.name, symbol.display_name)
                }))?;
                if total > offset + results.len() {
                    eprintln!(
                        "-- {} of {} results (use --offset {} for more)",
                        results.len(),
                        total,
                        offset + results.len()
                    );
                }
            }
            Ok(())
        }
    }
}

pub fn imports(ctx: &Context, file: &str, format: Format) -> anyhow::Result<()> {
    if let Err(err) = code_graph::require_graph_reads(ctx) {
        if graph_read_unavailable(&err) {
            return empty_paged_response::<crate::models::GraphResult>(ctx, 0, 0, format);
        }
        return Err(err);
    }
    let results = match code_graph::get_imports(ctx, file) {
        Ok(results) => results,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, 0, 0, format);
        }
        Err(err) => return Err(err),
    };
    let total = results.len();
    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: 0,
            limit: total,
            results,
            hint: hint_for(ctx),
        }),
        Format::Text => {
            if results.is_empty() {
                output::print_text(&format!("No imports found for '{file}'"))?;
                print_graph_hint_text(ctx);
            } else {
                for r in &results {
                    output::print_text(&r.name)?;
                }
            }
            Ok(())
        }
    }
}

pub fn blast_radius(
    ctx: &Context,
    target: &str,
    depth: usize,
    format: Format,
) -> anyhow::Result<()> {
    if let Err(err) = code_graph::require_graph_reads(ctx) {
        if graph_read_unavailable(&err) {
            return empty_paged_response::<crate::models::GraphResult>(ctx, 0, 0, format);
        }
        return Err(err);
    }
    let Some(symbol) = resolve_symbol_or_empty_response(ctx, target, 0, 0, format)? else {
        return Ok(());
    };
    let results = match code_graph::blast_radius(ctx, &symbol.id, depth) {
        Ok(results) => results,
        Err(err) if graph_read_unavailable(&err) => {
            return empty_paged_response::<crate::models::GraphResult>(ctx, 0, 0, format);
        }
        Err(err) => return Err(err),
    };
    let total = results.len();
    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: 0,
            limit: total,
            results,
            hint: hint_for(ctx),
        }),
        Format::Text => {
            if results.is_empty() {
                output::print_text(&format!(
                    "No blast radius found for '{}'",
                    symbol.display_name
                ))?;
                print_graph_hint_text(ctx);
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    let dist = r.distance.unwrap_or(0);
                    format!("{} [distance={}] {}", r.line, dist, r.name)
                }))?;
            }
            Ok(())
        }
    }
}
