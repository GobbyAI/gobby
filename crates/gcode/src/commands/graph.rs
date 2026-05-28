use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{
    self, GraphBlastRadiusTarget, GraphLifecycleAction, GraphLifecycleOutput, GraphPayload,
};
use crate::graph::report::{ProjectGraphReport, ProjectGraphReportOptions};
use crate::models::PagedResponse;
use crate::output::{self, Format};
use crate::projection::sync::ProjectionSyncReport;
use crate::search::fts::{self, ResolvedGraphSymbol};
use serde_json::{Value, json};

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
    let output = match action {
        GraphLifecycleAction::Clear => clear_project_graph(ctx)?,
        GraphLifecycleAction::Rebuild => rebuild_project_graph(ctx)?,
    };
    match format {
        Format::Json => output::print_json(&output.payload),
        Format::Text => {
            output::print_text(&format_success_text(&output))?;
            output::print_json_compact(&output.payload)
        }
    }
}

fn lifecycle_output(
    action: GraphLifecycleAction,
    ctx: &Context,
    payload: Value,
) -> GraphLifecycleOutput {
    let summary = code_graph::extract_summary_text(&payload).unwrap_or_else(|| payload.to_string());
    GraphLifecycleOutput {
        project_id: ctx.project_id.clone(),
        action,
        summary,
        payload,
    }
}

struct GraphFileSyncOutcome {
    relationships_written: usize,
    symbols_synced: usize,
}

fn sync_file_graph(ctx: &Context, file_path: &str) -> anyhow::Result<GraphFileSyncOutcome> {
    code_graph::require_graph_reads(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
    if !db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path)? {
        anyhow::bail!(
            "indexed file `{file_path}` was not found for project {}",
            ctx.project_id
        );
    }
    let relationships_written = code_graph::sync_file_graph(
        ctx,
        &facts.file_path,
        &facts.imports,
        &facts.definitions,
        &facts.calls,
    )?;
    db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
    Ok(GraphFileSyncOutcome {
        relationships_written,
        symbols_synced: facts.definitions.len(),
    })
}

fn clear_project_graph(ctx: &Context) -> anyhow::Result<GraphLifecycleOutput> {
    code_graph::require_graph_reads(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let files_marked_pending = db::reset_graph_sync_for_project(&mut conn, &ctx.project_id)?;
    code_graph::clear_project(ctx)?;
    let report = ProjectionSyncReport::ok(0, 0);
    Ok(lifecycle_output(
        GraphLifecycleAction::Clear,
        ctx,
        json!({
            "success": true,
            "project_id": ctx.project_id,
            "status": report.status,
            "synced_files": report.synced_files,
            "synced_symbols": report.synced_symbols,
            "degraded": report.degraded,
            "error": report.error,
            "files_marked_pending": files_marked_pending,
            "summary": format!("marked {files_marked_pending} files pending and cleared graph projection"),
        }),
    ))
}

fn rebuild_project_graph(ctx: &Context) -> anyhow::Result<GraphLifecycleOutput> {
    code_graph::require_graph_reads(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?;
    code_graph::clear_project(ctx)?;
    db::reset_graph_sync_for_project(&mut conn, &ctx.project_id)?;

    let mut files_synced = 0usize;
    let mut symbols_synced = 0usize;
    let mut errors = Vec::new();
    for file_path in &file_paths {
        let synced_symbols =
            match db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path)
                .and_then(|updated| {
                    if updated {
                        Ok(())
                    } else {
                        anyhow::bail!("indexed file no longer exists")
                    }
                })
                .and_then(|_| {
                    let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
                    code_graph::sync_file_graph(
                        ctx,
                        &facts.file_path,
                        &facts.imports,
                        &facts.definitions,
                        &facts.calls,
                    )?;
                    db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
                    Ok(facts.definitions.len())
                }) {
                Ok(symbols) => symbols,
                Err(err) => {
                    errors.push(format!("{file_path}: {err}"));
                    continue;
                }
            };
        files_synced += 1;
        symbols_synced += synced_symbols;
    }

    let report = if errors.is_empty() {
        ProjectionSyncReport::ok(files_synced, symbols_synced)
    } else {
        ProjectionSyncReport::degraded(
            "sync_failed",
            errors.join("; "),
            files_synced,
            symbols_synced,
        )
    };
    Ok(lifecycle_output(
        GraphLifecycleAction::Rebuild,
        ctx,
        json!({
            "success": true,
            "project_id": ctx.project_id,
            "status": report.status,
            "synced_files": report.synced_files,
            "synced_symbols": report.synced_symbols,
            "degraded": report.degraded,
            "error": report.error,
            "files_processed": file_paths.len(),
            "files_synced": files_synced,
            "files_failed": errors.len(),
            "errors": errors,
            "summary": format!("synced {files_synced}/{} files", file_paths.len()),
        }),
    ))
}

pub fn clear(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action(ctx, GraphLifecycleAction::Clear, format)
}

pub fn rebuild(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action(ctx, GraphLifecycleAction::Rebuild, format)
}

pub fn sync_file(ctx: &Context, file_path: &str, format: Format) -> anyhow::Result<()> {
    let sync = sync_file_graph(ctx, file_path)?;
    let relationships_written = sync.relationships_written;
    let report = ProjectionSyncReport::ok(1, sync.symbols_synced);
    let summary = format!("synced {relationships_written} graph relationships for {file_path}");
    let payload = json!({
        "success": true,
        "project_id": ctx.project_id,
        "file_path": file_path,
        "status": report.status,
        "synced_files": report.synced_files,
        "synced_symbols": report.synced_symbols,
        "degraded": report.degraded,
        "error": report.error,
        "relationships_written": relationships_written,
        "summary": summary,
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            output::print_text(&format!(
                "Synced code-index graph for project {}: {summary}",
                ctx.project_id
            ))?;
            output::print_json_compact(&payload)
        }
    }
}

fn format_graph_payload_text(payload: &GraphPayload) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "nodes: {}, links: {}",
        payload.nodes.len(),
        payload.links.len()
    ));
    if let Some(center) = &payload.center {
        lines.push(format!("center: {center}"));
    }
    for node in &payload.nodes {
        let file = node.file_path.as_deref().unwrap_or("");
        if file.is_empty() {
            lines.push(format!(
                "node {} [{}] {}",
                node.id, node.node_type, node.name
            ));
        } else {
            lines.push(format!(
                "node {} [{}] {} {}",
                node.id, node.node_type, node.name, file
            ));
        }
    }
    for link in &payload.links {
        lines.push(format!(
            "link {} -[{}]-> {}",
            link.source, link.link_type, link.target
        ));
    }
    lines.join("\n")
}

fn print_graph_payload(payload: &GraphPayload, format: Format) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(payload),
        Format::Text => output::print_text(&format_graph_payload_text(payload)),
    }
}

fn format_report_text(report: &ProjectGraphReport) -> anyhow::Result<String> {
    Ok(serde_json::to_string_pretty(report)?)
}

pub fn report(ctx: &Context, top_n: usize, format: Format) -> anyhow::Result<()> {
    let report = crate::graph::report::generate_report_with_options(
        ctx,
        ProjectGraphReportOptions { top_n },
    )?;
    match format {
        Format::Json => output::print_json(&report),
        Format::Text => output::print_text(&format_report_text(&report)?),
    }
}

pub fn overview(ctx: &Context, limit: usize, format: Format) -> anyhow::Result<()> {
    let payload = code_graph::project_overview_graph(ctx, limit)?;
    print_graph_payload(&payload, format)
}

pub fn file(ctx: &Context, file_path: &str, format: Format) -> anyhow::Result<()> {
    let payload = code_graph::file_graph(ctx, file_path)?;
    print_graph_payload(&payload, format)
}

pub fn neighbors(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    let payload = code_graph::symbol_neighbors(ctx, symbol_id, limit)?;
    print_graph_payload(&payload, format)
}

pub fn graph_blast_radius(
    ctx: &Context,
    symbol_id: Option<&str>,
    file_path: Option<&str>,
    depth: usize,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    let target = match (symbol_id, file_path) {
        (Some(symbol_id), None) => GraphBlastRadiusTarget::SymbolId(symbol_id.to_string()),
        (None, Some(file_path)) => GraphBlastRadiusTarget::FilePath(file_path.to_string()),
        _ => anyhow::bail!("provide exactly one of --symbol-id or --file"),
    };
    let payload = code_graph::blast_radius_graph(ctx, target, depth, limit)?;
    print_graph_payload(&payload, format)
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
    use crate::models::{GraphResult, ProjectionMetadata, ProjectionProvenance};
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
    fn report_text_structured_output() {
        let report = crate::graph::report::empty_report("project-123");

        let text = format_report_text(&report).expect("format report text");
        let value: serde_json::Value = serde_json::from_str(&text).expect("structured JSON text");

        assert_eq!(value["project_id"], "project-123");
        assert_eq!(value["summary"]["node_count"], 0);
        assert!(
            value["markdown"]
                .as_str()
                .expect("markdown field")
                .contains("# Project Graph Report")
        );
        assert!(!text.trim_start().starts_with('#'));
    }

    #[test]
    fn report_requires_graph_service() {
        let ctx = make_ctx_no_falkordb();

        let err = report(&ctx, 10, Format::Json).expect_err("report must fail");

        assert!(matches!(
            err.downcast_ref::<crate::graph::report::ProjectGraphReportError>(),
            Some(crate::graph::report::ProjectGraphReportError::GraphServiceNotConfigured)
        ));
        assert!(
            err.to_string()
                .contains("project graph report requires FalkorDB"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn graph_lifecycle_commands_call_core_directly() {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let source = std::fs::read_to_string(manifest_dir.join("src/commands/graph.rs"))
            .expect("read commands/graph.rs");
        let clear_project = ["code_graph", "::clear_project(ctx)"].concat();
        let sync_file_graph = ["code_graph", "::sync_file_graph("].concat();
        let lifecycle_request = ["GraphLifecycleRequest", "::from_context"].concat();
        let daemon_lifecycle = ["code_graph", "::run_lifecycle_action"].concat();

        assert!(source.contains(&clear_project));
        assert!(source.contains(&sync_file_graph));
        assert!(!source.contains(&lifecycle_request));
        assert!(!source.contains(&daemon_lifecycle));
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

    #[test]
    fn top_level_read_commands_preserve_json_shape() {
        let response = PagedResponse {
            project_id: "project-123".to_string(),
            total: 1,
            offset: 0,
            limit: 10,
            results: vec![GraphResult {
                id: "sym-1".to_string(),
                name: "run".to_string(),
                file_path: "src/lib.rs".to_string(),
                line: 12,
                relation: Some("CALLS".to_string()),
                distance: Some(1),
                metadata: None,
            }],
            hint: None,
        };

        let value = serde_json::to_value(&response).expect("serialize response");

        assert_eq!(value["project_id"], "project-123");
        assert_eq!(value["total"], 1);
        assert_eq!(value["offset"], 0);
        assert_eq!(value["limit"], 10);
        assert_eq!(value["results"][0]["id"], "sym-1");
        assert_eq!(value["results"][0]["name"], "run");
        assert_eq!(value["results"][0]["file_path"], "src/lib.rs");
        assert_eq!(value["results"][0]["line"], 12);
        assert_eq!(value["results"][0]["relation"], "CALLS");
        assert_eq!(value["results"][0]["distance"], 1);
        assert!(value["hint"].is_null());
        assert!(value["results"][0].get("metadata").is_none());

        let response = PagedResponse {
            project_id: "project-123".to_string(),
            total: 1,
            offset: 0,
            limit: 10,
            results: vec![GraphResult {
                id: "sym-1".to_string(),
                name: "run".to_string(),
                file_path: "src/lib.rs".to_string(),
                line: 12,
                relation: Some("CALLS".to_string()),
                distance: Some(1),
                metadata: Some(
                    ProjectionMetadata::new(ProjectionProvenance::Extracted, "gcode")
                        .with_source_file_path("src/lib.rs"),
                ),
            }],
            hint: None,
        };
        let value = serde_json::to_value(&response).expect("serialize metadata response");

        assert_eq!(
            value["results"][0]["metadata"]["source_file_path"],
            "src/lib.rs"
        );
    }
}
