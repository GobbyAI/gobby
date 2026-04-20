use anyhow::Context as _;
use reqwest::StatusCode;
use serde_json::Value;

use crate::config::Context;
use crate::db;
use crate::models::PagedResponse;
use crate::neo4j;
use crate::output::{self, Format};
use crate::search::fts::{self, ResolvedGraphSymbol};

const GOBBY_HINT: &str =
    "Graph commands require Neo4j, available with Gobby. See: https://github.com/GobbyAI/gobby";

#[derive(Clone, Copy, Debug)]
enum GraphLifecycleAction {
    Clear,
    Rebuild,
}

impl GraphLifecycleAction {
    fn cli_command(self) -> &'static str {
        match self {
            Self::Clear => "gcode graph clear",
            Self::Rebuild => "gcode graph rebuild",
        }
    }

    fn endpoint_path(self) -> &'static str {
        match self {
            Self::Clear => "/api/code-index/graph/clear",
            Self::Rebuild => "/api/code-index/graph/rebuild",
        }
    }

    fn success_prefix(self) -> &'static str {
        match self {
            Self::Clear => "Cleared code-index graph",
            Self::Rebuild => "Rebuilt code-index graph",
        }
    }
}

fn require_daemon_url(
    daemon_url: Option<&str>,
    action: GraphLifecycleAction,
) -> anyhow::Result<&str> {
    daemon_url.ok_or_else(|| {
        anyhow::anyhow!(
            "Gobby daemon URL is not configured. `{}` requires the Gobby daemon.",
            action.cli_command()
        )
    })
}

fn build_lifecycle_url(
    base_url: &str,
    action: GraphLifecycleAction,
    project_id: &str,
) -> anyhow::Result<reqwest::Url> {
    let base = base_url.trim_end_matches('/');
    let mut url = reqwest::Url::parse(&format!("{base}{}", action.endpoint_path()))
        .with_context(|| format!("invalid Gobby daemon URL: {base_url}"))?;
    url.query_pairs_mut().append_pair("project_id", project_id);
    Ok(url)
}

fn compact_detail(body: &str) -> String {
    let detail = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let detail = detail.trim();
    if detail.len() > 240 {
        format!("{}...", &detail[..237])
    } else {
        detail.to_string()
    }
}

fn format_http_error(
    action: GraphLifecycleAction,
    url: &reqwest::Url,
    status: StatusCode,
    body: &str,
) -> String {
    let detail = compact_detail(body);
    if detail.is_empty() {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}",
            action.cli_command()
        )
    } else {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}: {detail}",
            action.cli_command()
        )
    }
}

fn parse_success_payload(
    action: GraphLifecycleAction,
    status: StatusCode,
    body: &str,
) -> anyhow::Result<Value> {
    serde_json::from_str(body).map_err(|err| {
        let detail = compact_detail(body);
        if detail.is_empty() {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}",
                action.cli_command()
            )
        } else {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}. Response: {detail}",
                action.cli_command()
            )
        }
    })
}

fn extract_summary_text(payload: &Value) -> Option<String> {
    match payload {
        Value::String(text) => {
            let text = text.trim();
            (!text.is_empty()).then(|| text.to_string())
        }
        Value::Object(map) => ["summary", "message", "detail", "status"]
            .iter()
            .find_map(|key| map.get(*key).and_then(Value::as_str))
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .map(ToOwned::to_owned),
        _ => None,
    }
}

fn format_success_text(
    action: GraphLifecycleAction,
    project_id: &str,
    payload: &Value,
) -> anyhow::Result<String> {
    let detail = match extract_summary_text(payload) {
        Some(summary) => summary,
        None => serde_json::to_string(payload)?,
    };

    Ok(format!(
        "{} for project {}: {}",
        action.success_prefix(),
        project_id,
        detail
    ))
}

fn run_lifecycle_action(
    ctx: &Context,
    action: GraphLifecycleAction,
    format: Format,
) -> anyhow::Result<()> {
    let daemon_url = require_daemon_url(ctx.daemon_url.as_deref(), action)?;
    let url = build_lifecycle_url(daemon_url, action, &ctx.project_id)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .context("failed to build HTTP client")?;

    let response = client
        .post(url.clone())
        .header("Accept", "application/json")
        .send()
        .with_context(|| {
            format!(
                "Failed to reach Gobby daemon at {daemon_url} for `{}`",
                action.cli_command()
            )
        })?;

    let status = response.status();
    let body = response.text().unwrap_or_default();
    if !status.is_success() {
        anyhow::bail!("{}", format_http_error(action, &url, status, &body));
    }

    let payload = parse_success_payload(action, status, &body)?;
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            println!(
                "{}",
                format_success_text(action, &ctx.project_id, &payload)?
            );
            Ok(())
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
    let conn = match db::open_readonly(&ctx.db_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to open index for graph resolution: {e}");
            return None;
        }
    };
    let (resolved, suggestions) = fts::resolve_graph_symbol(&conn, input, &ctx.project_id);
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
    let symbol = match resolve_symbol(ctx, symbol_name) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let total = neo4j::count_callers(ctx, &symbol.id)?;
    let results = neo4j::find_callers(ctx, &symbol.id, offset, limit)?;

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
    let symbol = match resolve_symbol(ctx, symbol_name) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let total = neo4j::count_usages(ctx, &symbol.id)?;
    let results = neo4j::find_usages(ctx, &symbol.id, offset, limit)?;

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
    let symbol = match resolve_symbol(ctx, target) {
        Some(symbol) => symbol,
        None => return empty_response_for_unresolved(ctx, format),
    };
    let results = neo4j::blast_radius(ctx, &symbol.id, depth)?;
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

    #[test]
    fn test_build_lifecycle_url_clear_uses_project_id_query() {
        let url = build_lifecycle_url(
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
        let url = build_lifecycle_url(
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
        let err = require_daemon_url(None, GraphLifecycleAction::Clear).expect_err("must fail");

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
        let message = format_http_error(
            GraphLifecycleAction::Clear,
            &url,
            StatusCode::BAD_GATEWAY,
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
        let err = parse_success_payload(GraphLifecycleAction::Rebuild, StatusCode::OK, "not json")
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

        let text = format_success_text(GraphLifecycleAction::Clear, "project-123", &payload)
            .expect("text formats");

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

        let text = format_success_text(GraphLifecycleAction::Rebuild, "project-123", &payload)
            .expect("text formats");

        assert_eq!(
            text,
            "Rebuilt code-index graph for project project-123: {\"replayed\":18,\"synced\":18}"
        );
    }
}
