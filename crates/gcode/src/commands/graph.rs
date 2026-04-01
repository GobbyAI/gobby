use crate::config::Context;
use crate::models::PagedResponse;
use crate::neo4j;
use crate::output::{self, Format};

const GOBBY_HINT: &str =
    "Graph commands require Neo4j, available with Gobby. See: https://github.com/GobbyAI/gobby";

fn hint_for(ctx: &Context) -> Option<String> {
    if ctx.neo4j.is_none() {
        Some(GOBBY_HINT.to_string())
    } else {
        None
    }
}

fn print_graph_hint_text(ctx: &Context) {
    if ctx.neo4j.is_none() {
        eprintln!("Hint: {GOBBY_HINT}");
    }
}

pub fn callers(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    let total = neo4j::count_callers(ctx, symbol_name)?;
    let results = neo4j::find_callers(ctx, symbol_name, offset, limit)?;

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
                println!("No callers found for '{symbol_name}'");
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No callers at offset {offset} (total {total})");
            } else {
                for r in &results {
                    println!("{}:{} {} -> {}", r.file_path, r.line, r.name, symbol_name);
                }
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
    let total = neo4j::count_usages(ctx, symbol_name)?;
    let results = neo4j::find_usages(ctx, symbol_name, offset, limit)?;

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
                println!("No usages found for '{symbol_name}'");
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No usages at offset {offset} (total {total})");
            } else {
                for r in &results {
                    let rel = r.relation.as_deref().unwrap_or("unknown");
                    println!(
                        "{}:{} [{}] {} -> {}",
                        r.file_path, r.line, rel, r.name, symbol_name
                    );
                }
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
    let results = neo4j::get_imports(ctx, file)?;
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
                println!("No imports found for '{file}'");
                print_graph_hint_text(ctx);
            } else {
                for r in &results {
                    println!("{}", r.name);
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
    let results = neo4j::blast_radius(ctx, target, depth)?;
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
                println!("No blast radius found for '{target}'");
                print_graph_hint_text(ctx);
            } else {
                for r in &results {
                    let dist = r.distance.unwrap_or(0);
                    println!("{}:{} [distance={}] {}", r.file_path, r.line, dist, r.name);
                }
            }
            Ok(())
        }
    }
}
