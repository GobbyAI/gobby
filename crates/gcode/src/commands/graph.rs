use crate::config::Context;
use crate::neo4j;
use crate::output::{self, Format};

const GOBBY_HINT: &str =
    "Graph commands require Neo4j, available with Gobby. See: https://github.com/GobbyAI/gobby";

/// Print results with a hint when Neo4j is not configured and results are empty.
fn print_graph_json<T: serde::Serialize>(ctx: &Context, results: &[T]) -> anyhow::Result<()> {
    if results.is_empty() && ctx.neo4j.is_none() {
        let wrapped = serde_json::json!({
            "results": [],
            "hint": GOBBY_HINT,
        });
        println!("{}", serde_json::to_string_pretty(&wrapped)?);
        Ok(())
    } else {
        output::print_json(results)
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
    format: Format,
) -> anyhow::Result<()> {
    let results = neo4j::find_callers(ctx, symbol_name, limit)?;
    match format {
        Format::Json => print_graph_json(ctx, &results),
        Format::Text => {
            if results.is_empty() {
                println!("No callers found for '{symbol_name}'");
                print_graph_hint_text(ctx);
            } else {
                for r in &results {
                    println!("{}:{} {} -> {}", r.file_path, r.line, r.name, symbol_name);
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
    format: Format,
) -> anyhow::Result<()> {
    let results = neo4j::find_usages(ctx, symbol_name, limit)?;
    match format {
        Format::Json => print_graph_json(ctx, &results),
        Format::Text => {
            if results.is_empty() {
                println!("No usages found for '{symbol_name}'");
                print_graph_hint_text(ctx);
            } else {
                for r in &results {
                    let rel = r.relation.as_deref().unwrap_or("unknown");
                    println!(
                        "{}:{} [{}] {} -> {}",
                        r.file_path, r.line, rel, r.name, symbol_name
                    );
                }
            }
            Ok(())
        }
    }
}

pub fn imports(ctx: &Context, file: &str, format: Format) -> anyhow::Result<()> {
    let results = neo4j::get_imports(ctx, file)?;
    match format {
        Format::Json => print_graph_json(ctx, &results),
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
    match format {
        Format::Json => print_graph_json(ctx, &results),
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
