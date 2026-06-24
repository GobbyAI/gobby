use std::collections::BTreeMap;

use crate::commands::token_budget;
use crate::config::Context;
use crate::db;
use crate::graph::code_graph;
use crate::models::{GraphPathStep, GraphResult, PagedResponse};
use crate::output::{self, Format};
use crate::search::fts::{self, ResolvedGraphSymbol};
use serde::Serialize;

const GRAPH_BACKEND_HINT: &str =
    "Graph commands require a configured FalkorDB graph backend and synced graph projection.";
const USAGES_TOKEN_BUDGET_REFINE_HINT: &str =
    "`--limit`, `--offset`, or a more specific symbol query";
const BLAST_RADIUS_TOKEN_BUDGET_REFINE_HINT: &str =
    "`--depth`, a more specific symbol query, or a symbol UUID";

fn hint_for(ctx: &Context) -> Option<String> {
    if ctx.falkordb.is_none() {
        Some(GRAPH_BACKEND_HINT.to_string())
    } else {
        None
    }
}

fn hint_for_error(ctx: &Context, error: &anyhow::Error) -> Option<String> {
    match error.downcast_ref::<code_graph::GraphReadError>() {
        Some(code_graph::GraphReadError::NotConfigured) => hint_for(ctx),
        Some(code_graph::GraphReadError::Unreachable { message }) => Some(format!(
            "FalkorDB is configured but unreachable; graph results are unavailable: {message}"
        )),
        _ => hint_for(ctx),
    }
}

fn print_graph_hint_text(ctx: &Context, error: Option<&anyhow::Error>) {
    let hint = error.and_then(|err| hint_for_error(ctx, err));
    let hint = hint.or_else(|| hint_for(ctx));
    if let Some(hint) = hint {
        eprintln!("Hint: {hint}");
    }
}

fn print_hint_text(hint: Option<&str>) {
    if let Some(hint) = hint {
        eprintln!("Hint: {hint}");
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
    error: Option<&anyhow::Error>,
) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(&PagedResponse::<T> {
            project_id: ctx.project_id.clone(),
            total: 0,
            offset,
            limit,
            results: vec![],
            hint: error
                .and_then(|err| hint_for_error(ctx, err))
                .or_else(|| hint_for(ctx)),
        }),
        Format::Text => {
            print_graph_hint_text(ctx, error);
            Ok(())
        }
    }
}

fn graph_read_or_empty<T: Serialize>(
    ctx: &Context,
    offset: usize,
    limit: usize,
    format: Format,
    read: impl FnOnce() -> anyhow::Result<T>,
) -> anyhow::Result<Option<T>> {
    match read() {
        Ok(value) => Ok(Some(value)),
        Err(err) if graph_read_unavailable(&err) => {
            empty_paged_response::<T>(ctx, offset, limit, format, Some(&err))?;
            Ok(None)
        }
        Err(err) => Err(err),
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

pub(super) fn format_caller_result_line(result: &GraphResult, target_name: &str) -> String {
    format!(
        "{} [{}] {} -> {}",
        result.line, result.confidence, result.name, target_name
    )
}

pub(super) fn format_usage_result_line(result: &GraphResult, target_name: &str) -> String {
    let rel = result.relation.as_deref().unwrap_or("unknown");
    format!(
        "{} [{}] [{}] {} -> {}",
        result.line, result.confidence, rel, result.name, target_name
    )
}

pub(super) fn format_blast_radius_result_line(result: &GraphResult) -> String {
    let distance = result.distance.unwrap_or(0);
    format!(
        "{} [{}] [distance={}] {}",
        result.line, result.confidence, distance, result.name
    )
}

#[derive(Serialize)]
struct GraphPathEndpoint {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<String>,
    display_name: String,
}

#[derive(Serialize)]
struct GraphPathResponse {
    project_id: String,
    found: bool,
    from: GraphPathEndpoint,
    to: GraphPathEndpoint,
    max_depth: usize,
    hops: Option<usize>,
    path: Vec<GraphPathStep>,
    #[serde(skip_serializing_if = "Option::is_none")]
    hint: Option<String>,
}

fn path_endpoint(input: &str, resolved: Option<&ResolvedGraphSymbol>) -> GraphPathEndpoint {
    GraphPathEndpoint {
        id: resolved.map(|symbol| symbol.id.clone()),
        display_name: resolved
            .map(|symbol| symbol.display_name.clone())
            .unwrap_or_else(|| input.to_string()),
    }
}

pub(super) fn format_symbol_path_text(
    from_display: &str,
    to_display: &str,
    path: &[GraphPathStep],
    max_depth: usize,
) -> String {
    if path.is_empty() {
        return format!(
            "No path found from '{from_display}' to '{to_display}' within {max_depth} CALLS hop(s)."
        );
    }

    let hops = path.len().saturating_sub(1);
    let mut lines = vec![format!(
        "Shortest path from '{from_display}' to '{to_display}' ({hops} hop(s)):"
    )];
    for step in path {
        let file_path = if step.file_path.is_empty() {
            "<unknown>"
        } else {
            &step.file_path
        };
        lines.push(format!(
            "{}. {} ({}:{})",
            step.position + 1,
            step.name,
            file_path,
            step.line
        ));
    }
    lines.join("\n")
}

fn print_symbol_path_response(
    ctx: &Context,
    from: GraphPathEndpoint,
    to: GraphPathEndpoint,
    max_depth: usize,
    path: Vec<GraphPathStep>,
    format: Format,
) -> anyhow::Result<()> {
    let found = !path.is_empty();
    let response = GraphPathResponse {
        project_id: ctx.project_id.clone(),
        found,
        hops: found.then_some(path.len().saturating_sub(1)),
        from,
        to,
        max_depth,
        path,
        hint: hint_for(ctx),
    };

    match format {
        Format::Json => output::print_json(&response),
        Format::Text => {
            output::print_text(&format_symbol_path_text(
                &response.from.display_name,
                &response.to.display_name,
                &response.path,
                max_depth,
            ))?;
            if !response.found {
                print_graph_hint_text(ctx, None);
            }
            Ok(())
        }
    }
}

fn resolve_symbol_with_connection(
    conn: &mut postgres::Client,
    project_id: &str,
    input: &str,
) -> anyhow::Result<(Option<ResolvedGraphSymbol>, Vec<String>)> {
    if let Ok(symbol_id) = uuid::Uuid::parse_str(input) {
        return Ok((
            fts::resolve_graph_symbol_by_id(conn, &symbol_id.to_string(), project_id)?,
            Vec::new(),
        ));
    }

    fts::resolve_graph_symbol(conn, input, project_id)
}

fn resolve_symbol_candidates(
    ctx: &Context,
    input: &str,
) -> anyhow::Result<(Option<ResolvedGraphSymbol>, Vec<String>)> {
    let mut conn = match db::connect_readonly(&ctx.database_url) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to open index for graph resolution: {e}");
            return Ok((None, Vec::new()));
        }
    };
    resolve_symbol_with_connection(&mut conn, &ctx.project_id, input)
}

fn print_symbol_resolution_failure(input: &str, suggestions: &[String]) {
    if suggestions.is_empty() {
        eprintln!("No symbol matching '{input}' found");
    } else {
        eprintln!(
            "Ambiguous symbol '{input}'. Refine the query. Matches: {}",
            suggestions.join(", ")
        );
    }
}

/// Resolve user input to a canonical symbol id, printing suggestions on ambiguity.
/// Returns None and prints an error message if no match found.
fn resolve_symbol(ctx: &Context, input: &str) -> anyhow::Result<Option<ResolvedGraphSymbol>> {
    let (resolved, suggestions) = resolve_symbol_candidates(ctx, input)?;
    if resolved.is_none() {
        print_symbol_resolution_failure(input, &suggestions);
    }
    Ok(resolved)
}

fn resolve_blast_radius_target(
    ctx: &Context,
    input: &str,
) -> anyhow::Result<Option<ResolvedGraphSymbol>> {
    let (resolved, suggestions) = resolve_symbol_candidates(ctx, input)?;
    if let Some(symbol) = resolved {
        return Ok(Some(symbol));
    }
    if !suggestions.is_empty() {
        print_symbol_resolution_failure(input, &suggestions);
        return Ok(None);
    }

    let (external, external_suggestions) = code_graph::resolve_external_call_target(ctx, input)?;
    if let Some(target) = external {
        return Ok(Some(ResolvedGraphSymbol {
            id: target.id,
            display_name: target.display_name,
        }));
    }
    if external_suggestions.is_empty() {
        eprintln!("No symbol or external call target matching '{input}' found");
    } else {
        eprintln!(
            "Ambiguous external call target '{input}'. Refine the query. Matches: {}",
            external_suggestions.join(", ")
        );
    }
    Ok(None)
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
            empty_paged_response::<crate::models::GraphResult>(ctx, offset, limit, format, None)?;
            Ok(None)
        }
    }
}

fn read_paged_symbol_graph_results(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
    count: impl FnOnce(&Context, &str) -> anyhow::Result<usize>,
    find: impl FnOnce(&Context, &str, usize, usize) -> anyhow::Result<Vec<GraphResult>>,
) -> anyhow::Result<Option<(ResolvedGraphSymbol, usize, Vec<GraphResult>)>> {
    let Some(()) = graph_read_or_empty::<()>(ctx, offset, limit, format, || {
        code_graph::require_graph_reads(ctx)
    })?
    else {
        return Ok(None);
    };
    let Some(symbol) = resolve_symbol_or_empty_response(ctx, symbol_name, offset, limit, format)?
    else {
        return Ok(None);
    };
    let Some(total) =
        graph_read_or_empty::<usize>(ctx, offset, limit, format, || count(ctx, &symbol.id))?
    else {
        return Ok(None);
    };
    let Some(results) =
        graph_read_or_empty::<Vec<GraphResult>>(ctx, offset, limit, format, || {
            find(ctx, &symbol.id, offset, limit)
        })?
    else {
        return Ok(None);
    };

    Ok(Some((symbol, total, results)))
}

pub fn callers(
    ctx: &Context,
    symbol_name: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    let Some((symbol, total, results)) = read_paged_symbol_graph_results(
        ctx,
        symbol_name,
        limit,
        offset,
        format,
        code_graph::count_callers,
        code_graph::find_callers,
    )?
    else {
        return Ok(());
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
                print_graph_hint_text(ctx, None);
            } else if results.is_empty() {
                eprintln!("No callers at offset {offset} (total {total})");
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    format_caller_result_line(r, &symbol.display_name)
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
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let Some((symbol, total, results)) = read_paged_symbol_graph_results(
        ctx,
        symbol_name,
        limit,
        offset,
        format,
        code_graph::count_usages,
        code_graph::find_usages,
    )?
    else {
        return Ok(());
    };
    let unbudgeted_result_count = results.len();
    let budgeted = token_budget::trim_results(
        results,
        token_budget,
        USAGES_TOKEN_BUDGET_REFINE_HINT,
        |result| format_usage_result_line(result, &symbol.display_name),
    );
    let results = budgeted.results;
    let hint = token_budget::combine_hints(hint_for(ctx), budgeted.hint);

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint,
        }),
        Format::Text => {
            if unbudgeted_result_count == 0 && offset == 0 {
                output::print_text(&format!("No usages found for '{}'", symbol.display_name))?;
                print_graph_hint_text(ctx, None);
            } else if unbudgeted_result_count == 0 {
                eprintln!("No usages at offset {offset} (total {total})");
            } else if results.is_empty() {
                print_hint_text(hint.as_deref());
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    format_usage_result_line(r, &symbol.display_name)
                }))?;
                print_hint_text(hint.as_deref());
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
    let Some(()) =
        graph_read_or_empty::<()>(ctx, 0, 0, format, || code_graph::require_graph_reads(ctx))?
    else {
        return Ok(());
    };
    let Some(results) =
        graph_read_or_empty::<Vec<crate::models::GraphResult>>(ctx, 0, 0, format, || {
            code_graph::get_imports(ctx, file)
        })?
    else {
        return Ok(());
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
                print_graph_hint_text(ctx, None);
            } else {
                for r in &results {
                    output::print_text(&r.name)?;
                }
            }
            Ok(())
        }
    }
}

pub fn path(
    ctx: &Context,
    symbol_a: &str,
    symbol_b: &str,
    max_depth: usize,
    format: Format,
) -> anyhow::Result<()> {
    let from = resolve_symbol(ctx, symbol_a)?;
    let to = resolve_symbol(ctx, symbol_b)?;
    let from_endpoint = path_endpoint(symbol_a, from.as_ref());
    let to_endpoint = path_endpoint(symbol_b, to.as_ref());
    let max_depth = max_depth.clamp(1, code_graph::MAX_SYMBOL_PATH_DEPTH);

    let path = match (&from, &to) {
        (Some(from), Some(to)) => {
            code_graph::shortest_symbol_path(ctx, &from.id, &to.id, max_depth)?
        }
        _ => Vec::new(),
    };

    print_symbol_path_response(ctx, from_endpoint, to_endpoint, max_depth, path, format)
}

pub fn blast_radius(
    ctx: &Context,
    target: &str,
    depth: usize,
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let Some(()) =
        graph_read_or_empty::<()>(ctx, 0, 0, format, || code_graph::require_graph_reads(ctx))?
    else {
        return Ok(());
    };
    let Some(symbol) = resolve_blast_radius_target(ctx, target)? else {
        empty_paged_response::<crate::models::GraphResult>(ctx, 0, 0, format, None)?;
        return Ok(());
    };
    let Some(results) =
        graph_read_or_empty::<Vec<crate::models::GraphResult>>(ctx, 0, 0, format, || {
            code_graph::blast_radius(ctx, &symbol.id, depth)
        })?
    else {
        return Ok(());
    };
    let total = results.len();
    let budgeted = token_budget::trim_results(
        results,
        token_budget,
        BLAST_RADIUS_TOKEN_BUDGET_REFINE_HINT,
        format_blast_radius_result_line,
    );
    let results = budgeted.results;
    let hint = token_budget::combine_hints(hint_for(ctx), budgeted.hint);
    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: 0,
            limit: total,
            results,
            hint,
        }),
        Format::Text => {
            if total == 0 {
                output::print_text(&format!(
                    "No blast radius found for '{}'",
                    symbol.display_name
                ))?;
                print_graph_hint_text(ctx, None);
            } else if results.is_empty() {
                print_hint_text(hint.as_deref());
            } else {
                output::print_text(&format_grouped_graph_results(&results, |r| {
                    format_blast_radius_result_line(r)
                }))?;
                print_hint_text(hint.as_deref());
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use postgres::Client;
    use postgres::types::ToSql;
    use std::time::{SystemTime, UNIX_EPOCH};

    const GRAPH_RESOLUTION_CHILD_TABLES: &[&str] = &[
        "code_calls",
        "code_imports",
        "code_symbols",
        "code_content_chunks",
        "code_indexed_files",
    ];

    fn graph_resolution_database_url() -> String {
        crate::test_env::postgres_test_database_url("graph resolution tests")
    }

    fn connect_graph_resolution_test_db() -> Client {
        let database_url = graph_resolution_database_url();
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
            .expect("connect graph resolution PostgreSQL test database");
        crate::schema::validate_runtime_schema(&mut conn)
            .expect("graph resolution PostgreSQL test schema is valid");
        conn
    }

    fn unique_uuid(label: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before unix epoch")
            .as_nanos();
        let key = format!("graph-resolution-test:{label}:{nanos}");
        uuid::Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
    }

    struct GraphResolutionProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl GraphResolutionProjectCleanup {
        fn new(project_id: &str) -> Self {
            Self {
                database_url: graph_resolution_database_url(),
                project_id: project_id.to_string(),
            }
        }
    }

    impl Drop for GraphResolutionProjectCleanup {
        fn drop(&mut self) {
            match gobby_core::postgres::connect_readwrite(&self.database_url) {
                Ok(mut conn) => {
                    if let Err(error) =
                        try_cleanup_graph_resolution_project(&mut conn, &self.project_id)
                    {
                        eprintln!("graph resolution cleanup failed: {error}");
                    }
                }
                Err(error) => eprintln!("graph resolution cleanup connect failed: {error}"),
            }
        }
    }

    fn cleanup_graph_resolution_project(conn: &mut Client, project_id: &str) {
        try_cleanup_graph_resolution_project(conn, project_id)
            .expect("cleanup graph resolution project");
    }

    fn try_cleanup_graph_resolution_project(
        conn: &mut Client,
        project_id: &str,
    ) -> Result<(), postgres::Error> {
        let mut tx = conn.transaction()?;
        for table in GRAPH_RESOLUTION_CHILD_TABLES {
            let sql = format!("DELETE FROM {table} WHERE project_id = $1");
            tx.execute(&sql, &[&project_id])?;
        }
        tx.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?;
        tx.commit()
    }

    fn insert_project(conn: &mut Client, project_id: &str) {
        let root_path = format!("/tmp/gcode-graph-resolution-{project_id}");
        conn.execute(
            "INSERT INTO code_indexed_projects
                (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
             VALUES ($1, $2, 0, 0, NOW(), 0)",
            &[&project_id, &root_path],
        )
        .expect("insert graph resolution project");
    }

    fn insert_file(conn: &mut Client, project_id: &str, file_path: &str, symbol_count: i32) {
        let id = format!("{project_id}:{file_path}");
        let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &symbol_count];
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', 'hash', $4, 1, false, false, NULL, NOW())",
            params,
        )
        .expect("insert graph resolution file");
    }

    fn insert_symbol(
        conn: &mut Client,
        project_id: &str,
        file_path: &str,
        id: &str,
        name: &str,
        line_start: i32,
    ) {
        let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &name, &line_start];
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $4, 'function', 'rust', 0, 1, $5, $5, $4, NULL, NULL,
                     'hash', NULL, NOW(), NOW())",
            params,
        )
        .expect("insert graph resolution symbol");
    }

    mod serial_db {
        use super::*;

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn uuid_input_resolves_exact_symbol_for_active_project() {
            let mut conn = connect_graph_resolution_test_db();
            let project_id = unique_uuid("project");
            cleanup_graph_resolution_project(&mut conn, &project_id);
            let _cleanup = GraphResolutionProjectCleanup::new(&project_id);
            insert_project(&mut conn, &project_id);
            insert_file(&mut conn, &project_id, "src/target.rs", 1);

            let symbol_id = unique_uuid("target-symbol");
            insert_symbol(
                &mut conn,
                &project_id,
                "src/target.rs",
                &symbol_id,
                "target_symbol",
                7,
            );

            let (resolved, suggestions) =
                resolve_symbol_with_connection(&mut conn, &project_id, &symbol_id)
                    .expect("resolve graph symbol by uuid");

            assert!(suggestions.is_empty());
            let resolved = resolved.expect("symbol should resolve");
            assert_eq!(resolved.id, symbol_id);
            assert_eq!(resolved.display_name, "target_symbol");
        }

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn unknown_uuid_input_does_not_fall_back_to_name_resolution() {
            let mut conn = connect_graph_resolution_test_db();
            let project_id = unique_uuid("project");
            cleanup_graph_resolution_project(&mut conn, &project_id);
            let _cleanup = GraphResolutionProjectCleanup::new(&project_id);
            insert_project(&mut conn, &project_id);
            insert_file(&mut conn, &project_id, "src/name.rs", 1);

            let uuid_shaped_name = unique_uuid("uuid-shaped-name");
            insert_symbol(
                &mut conn,
                &project_id,
                "src/name.rs",
                &unique_uuid("different-symbol-id"),
                &uuid_shaped_name,
                3,
            );

            let (resolved, suggestions) =
                resolve_symbol_with_connection(&mut conn, &project_id, &uuid_shaped_name)
                    .expect("resolve unknown uuid");

            assert!(resolved.is_none());
            assert!(suggestions.is_empty());
        }

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn ambiguous_name_behavior_remains_unchanged() {
            let mut conn = connect_graph_resolution_test_db();
            let project_id = unique_uuid("project");
            cleanup_graph_resolution_project(&mut conn, &project_id);
            let _cleanup = GraphResolutionProjectCleanup::new(&project_id);
            insert_project(&mut conn, &project_id);
            insert_file(&mut conn, &project_id, "src/a.rs", 1);
            insert_file(&mut conn, &project_id, "src/b.rs", 1);

            insert_symbol(
                &mut conn,
                &project_id,
                "src/a.rs",
                &unique_uuid("shared-a"),
                "shared_lookup",
                10,
            );
            insert_symbol(
                &mut conn,
                &project_id,
                "src/b.rs",
                &unique_uuid("shared-b"),
                "shared_lookup",
                20,
            );

            let (resolved, suggestions) =
                resolve_symbol_with_connection(&mut conn, &project_id, "shared_lookup")
                    .expect("resolve ambiguous name");

            assert!(resolved.is_none());
            assert_eq!(suggestions.len(), 2);
            assert!(suggestions.iter().any(|item| item.contains("src/a.rs:10")));
            assert!(suggestions.iter().any(|item| item.contains("src/b.rs:20")));
        }
    }
}
