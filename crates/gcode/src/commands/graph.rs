use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::models::PagedResponse;
use crate::output::{self, Format};
use crate::search::fts::{self, ResolvedGraphSymbol};

const GOBBY_HINT: &str =
    "Graph commands require FalkorDB, available with Gobby. See: https://github.com/GobbyAI/gobby";

fn format_success_text(output: &GraphLifecycleOutput) -> String {
    format!(
        "{} for project {}: {}",
        output.action.success_prefix(),
        output.project_id,
        output.summary
    )
}

fn run_lifecycle_action(
    ctx: &Context,
    action: GraphLifecycleAction,
    format: Format,
) -> anyhow::Result<()> {
    let request = code_graph::GraphLifecycleRequest::from_context(ctx);
    let output = code_graph::run_lifecycle_action(&request, action)?;
    match format {
        Format::Json => output::print_json(&output.payload),
        Format::Text => {
            eprintln!("{}", format_success_text(&output));
            output::print_json_compact(&output.payload)
        }
    }
}

pub fn clear(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action(ctx, GraphLifecycleAction::Clear, format)
}

pub fn rebuild(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action(ctx, GraphLifecycleAction::Rebuild, format)
}

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

fn empty_response_for_unresolved(ctx: &Context, format: Format) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(&PagedResponse::<Vec<()>> {
            project_id: ctx.project_id.clone(),
            total: 0,
            offset: 0,
            limit: 0,
            results: vec![],
            hint: hint_for(ctx),
        }),
        Format::Text => Ok(()),
    }
}

/// Resolve user input to a canonical symbol id, printing suggestions on ambiguity.
/// Returns None and prints an error message if no match found.
fn resolve_symbol(ctx: &Context, input: &str) -> Option<ResolvedGraphSymbol> {
    let mut conn = match db::connect_readonly(&ctx.database_url) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to open index for graph resolution: {e}");
            return None;
        }
    };
    let (resolved, suggestions) = fts::resolve_graph_symbol(&mut conn, input, &ctx.project_id);
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
    resolved
}

pub fn callers(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    code_graph::require_graph_reads(ctx)?;
    let symbol = match resolve_symbol(ctx, symbol_name) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let total = code_graph::count_callers(ctx, &symbol.id)?;
    let results = code_graph::find_callers(ctx, &symbol.id, offset, limit)?;

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
                println!("No callers found for '{}'", symbol.display_name);
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No callers at offset {offset} (total {total})");
            } else {
                for r in &results {
                    println!(
                        "{}:{} {} -> {}",
                        r.file_path, r.line, r.name, symbol.display_name
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

pub fn usages(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    code_graph::require_graph_reads(ctx)?;
    let symbol = match resolve_symbol(ctx, symbol_name) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let total = code_graph::count_usages(ctx, &symbol.id)?;
    let results = code_graph::find_usages(ctx, &symbol.id, offset, limit)?;

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
                println!("No usages found for '{}'", symbol.display_name);
                print_graph_hint_text(ctx);
            } else if results.is_empty() {
                eprintln!("No usages at offset {offset} (total {total})");
            } else {
                for r in &results {
                    let rel = r.relation.as_deref().unwrap_or("unknown");
                    println!(
                        "{}:{} [{}] {} -> {}",
                        r.file_path, r.line, rel, r.name, symbol.display_name
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
    code_graph::require_graph_reads(ctx)?;
    let results = code_graph::get_imports(ctx, file)?;
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
    code_graph::require_graph_reads(ctx)?;
    let symbol = match resolve_symbol(ctx, target) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let results = code_graph::blast_radius(ctx, &symbol.id, depth)?;
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
                println!("No blast radius found for '{}'", symbol.display_name);
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn make_ctx_no_falkordb() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "test-project".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            daemon_url: None,
        }
    }

    #[test]
    fn graph_reads_require_falkor() {
        let ctx = make_ctx_no_falkordb();

        let err = imports(&ctx, "src/lib.rs", Format::Json).expect_err("imports must fail");

        assert!(matches!(
            err.downcast_ref::<code_graph::GraphReadError>(),
            Some(code_graph::GraphReadError::NotConfigured)
        ));
        assert!(
            err.to_string().contains("FalkorDB is not configured"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn test_build_lifecycle_url_clear_uses_project_id_query() {
        let url = code_graph::build_lifecycle_url(
            "http://localhost:60887/",
            GraphLifecycleAction::Clear,
            "project-123",
        )
        .expect("url builds");

        assert_eq!(
            url.as_str(),
            "http://localhost:60887/api/code-index/graph/clear?project_id=project-123"
        );
    }

    #[test]
    fn test_build_lifecycle_url_rebuild_uses_project_id_query() {
        let url = code_graph::build_lifecycle_url(
            "http://localhost:60887",
            GraphLifecycleAction::Rebuild,
            "project-123",
        )
        .expect("url builds");

        assert_eq!(
            url.as_str(),
            "http://localhost:60887/api/code-index/graph/rebuild?project_id=project-123"
        );
    }

    #[test]
    fn test_require_daemon_url_errors_when_missing() {
        let err = code_graph::require_daemon_url(None, GraphLifecycleAction::Clear)
            .expect_err("must fail");

        assert!(
            err.to_string()
                .contains("Gobby daemon URL is not configured"),
            "unexpected error: {err}"
        );
        assert!(
            err.to_string().contains("gcode graph clear"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn test_format_http_error_includes_status_and_body() {
        let url = reqwest::Url::parse("http://localhost:60887/api/code-index/graph/clear")
            .expect("valid url");
        let message = code_graph::format_http_error(
            GraphLifecycleAction::Clear,
            &url,
            reqwest::StatusCode::BAD_GATEWAY,
            "daemon upstream unavailable",
        );

        assert!(message.contains("HTTP 502"), "unexpected error: {message}");
        assert!(
            message.contains("daemon upstream unavailable"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn test_parse_success_payload_fails_on_invalid_json() {
        let err = code_graph::parse_success_payload(
            GraphLifecycleAction::Rebuild,
            reqwest::StatusCode::OK,
            "not json",
        )
        .expect_err("invalid json must fail");

        assert!(
            err.to_string().contains("invalid JSON"),
            "unexpected error: {err}"
        );
        assert!(
            err.to_string().contains("HTTP 200 OK"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn test_format_success_text_prefers_message_field() {
        let payload = json!({
            "message": "cleared 12 graph nodes",
            "removed_nodes": 12
        });
        let output = GraphLifecycleOutput {
            project_id: "project-123".to_string(),
            action: GraphLifecycleAction::Clear,
            summary: "cleared 12 graph nodes".to_string(),
            payload,
        };
        let text = format_success_text(&output);

        assert_eq!(
            text,
            "Cleared code-index graph for project project-123: cleared 12 graph nodes"
        );
    }

    #[test]
    fn test_format_success_text_falls_back_to_compact_json() {
        let payload = json!({
            "replayed": 18,
            "synced": 18
        });
        let output = GraphLifecycleOutput {
            project_id: "project-123".to_string(),
            action: GraphLifecycleAction::Rebuild,
            summary: payload.to_string(),
            payload,
        };
        let text = format_success_text(&output);

        assert_eq!(
            text,
            "Rebuilt code-index graph for project project-123: {\"replayed\":18,\"synced\":18}"
        );
    }
}
