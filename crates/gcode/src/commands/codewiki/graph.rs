use super::*;
use crate::config::Context;
use gobby_core::falkor::{GraphClient, Row};
use std::time::Duration;

pub(crate) fn fetch_codewiki_graph_edges(
    ctx: &Context,
    files: &[String],
    symbols: &[Symbol],
    edge_limit: usize,
) -> anyhow::Result<CodewikiGraph> {
    let core_symbol_ids = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| symbol.id.clone())
        .collect::<HashSet<_>>();
    if core_symbol_ids.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let Some(config) = &ctx.falkordb else {
        return Ok(CodewikiGraph::unavailable());
    };

    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();

    // All FalkorDB I/O (connect + queries) runs under a wall-clock bound: a
    // stalled or half-open connection (FalkorDB mid-restart, redis "unexpected
    // end of file") must degrade to an unavailable graph, never freeze the whole
    // codewiki run. falkordb 0.2 exposes no client-side socket read timeout, so
    // the bound is enforced here on a detached worker thread.
    let Some(raw) = fetch_graph_rows_bounded(
        config.connection_config(),
        config.graph_name.clone(),
        ctx.project_id.clone(),
        edge_limit,
        !core_files.is_empty(),
        ctx.quiet,
        GRAPH_FETCH_TIMEOUT,
    ) else {
        return Ok(CodewikiGraph::unavailable());
    };

    let mut edges = Vec::new();
    // FalkorDB only reports that at most LIMIT rows were returned, so equality
    // is the conservative signal that additional rows may have been omitted.
    let mut truncated = raw.call_truncated;
    for (source, target) in raw.call_pairs {
        if !core_symbol_ids.contains(&source) {
            continue;
        }
        if !core_symbol_ids.contains(&target) {
            continue;
        }
        edges.push(CodewikiGraphEdge::call(source, target));
    }

    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        // A full import page may be exactly complete or may have hidden rows;
        // mark it truncated so rendered docs disclose that uncertainty.
        truncated |= raw.import_truncated;
        edges.extend(import_edges_from_pairs(
            &raw.import_pairs,
            &core_files,
            &file_symbols,
        ));
    }

    if truncated {
        Ok(CodewikiGraph::truncated(edges))
    } else {
        Ok(CodewikiGraph::available(edges))
    }
}

/// Wall-clock bound for the codewiki FalkorDB graph read. Simple `MATCH … LIMIT`
/// reads return in well under a second on a healthy graph; the generous bound
/// only trips when the connection stalls, in which case codewiki degrades to an
/// unavailable graph instead of freezing.
const GRAPH_FETCH_TIMEOUT: Duration = Duration::from_secs(30);

/// Raw `(source, target)` pairs pulled from FalkorDB before core-membership
/// filtering, plus the per-query truncation signals.
struct RawGraphRows {
    call_pairs: Vec<(String, String)>,
    import_pairs: Vec<(String, String)>,
    call_truncated: bool,
    import_truncated: bool,
}

/// Run `work` on a detached worker thread and return its value, or `on_timeout`
/// if it does not finish within `timeout`. A worker that overruns is abandoned
/// (it unwinds when its blocking I/O finally errors, or dies at process exit)
/// rather than joined, so a hung FalkorDB socket can never block the caller.
fn run_bounded<T: Send + 'static>(
    timeout: Duration,
    work: impl FnOnce() -> T + Send + 'static,
    on_timeout: impl FnOnce() -> T,
) -> T {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(work());
    });
    match rx.recv_timeout(timeout) {
        Ok(value) => value,
        Err(_) => on_timeout(),
    }
}

/// Fetch the codewiki graph rows under `timeout`, degrading to `None`
/// (unavailable) on connection failure, query failure, or timeout.
fn fetch_graph_rows_bounded(
    connection_config: gobby_core::config::FalkorConfig,
    graph_name: String,
    project_id: String,
    edge_limit: usize,
    need_import: bool,
    quiet: bool,
    timeout: Duration,
) -> Option<RawGraphRows> {
    run_bounded(
        timeout,
        move || {
            fetch_graph_rows(
                &connection_config,
                &graph_name,
                &project_id,
                edge_limit,
                need_import,
                quiet,
            )
        },
        move || {
            if !quiet {
                eprintln!(
                    "Warning: FalkorDB graph read timed out after {}s; codewiki \
                     continues without graph edges",
                    timeout.as_secs()
                );
            }
            None
        },
    )
}

/// Connect to FalkorDB and pull the call/import edge rows. Any connection or
/// query error degrades to `None`; the caller treats that as an unavailable
/// graph. Runs entirely on the worker thread spawned by [`run_bounded`].
fn fetch_graph_rows(
    connection_config: &gobby_core::config::FalkorConfig,
    graph_name: &str,
    project_id: &str,
    edge_limit: usize,
    need_import: bool,
    quiet: bool,
) -> Option<RawGraphRows> {
    let mut client = match GraphClient::from_config(connection_config, graph_name) {
        Ok(client) => client,
        Err(e) => {
            if !quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return None;
        }
    };

    let (query, params) = codewiki_call_edges_query(project_id, edge_limit);
    let call_rows = match client.query(&query, Some(params)) {
        Ok(rows) => rows,
        Err(e) => {
            if !quiet {
                eprintln!("Warning: FalkorDB query failed: {e}");
            }
            return None;
        }
    };
    let call_truncated = call_rows.len() == edge_limit;
    let call_pairs = rows_to_pairs(&call_rows);

    let (import_pairs, import_truncated) = if need_import {
        let (query, params) = codewiki_import_edges_query(project_id, edge_limit);
        let import_rows = match client.query(&query, Some(params)) {
            Ok(rows) => rows,
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: FalkorDB query failed: {e}");
                }
                return None;
            }
        };
        let truncated = import_rows.len() == edge_limit;
        (rows_to_pairs(&import_rows), truncated)
    } else {
        (Vec::new(), false)
    };

    Some(RawGraphRows {
        call_pairs,
        import_pairs,
        call_truncated,
        import_truncated,
    })
}

/// Extract `(source, target)` string pairs from FalkorDB rows, skipping any row
/// missing either column.
fn rows_to_pairs(rows: &[Row]) -> Vec<(String, String)> {
    rows.iter()
        .filter_map(|row| {
            let source = row.get("source").and_then(|value| value.as_str())?;
            let target = row.get("target").and_then(|value| value.as_str())?;
            Some((source.to_string(), target.to_string()))
        })
        .collect()
}

/// Resolve project-scoped import rows into component edges, keeping only
/// edges whose source file is core (the query itself is unfiltered).
pub(crate) fn import_edges_from_pairs(
    pairs: &[(String, String)],
    core_files: &[String],
    file_symbols: &BTreeMap<String, Vec<String>>,
) -> Vec<CodewikiGraphEdge> {
    let core_file_set = core_files
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let mut edges = Vec::new();
    for (source_file, target_module) in pairs {
        if !core_file_set.contains(source_file.as_str()) {
            continue;
        }
        let Some(source_component_id) = first_component_for_file(file_symbols, source_file) else {
            continue;
        };
        for target_file in files_for_import_target(core_files, target_module) {
            let Some(target_component_id) = first_component_for_file(file_symbols, target_file)
            else {
                continue;
            };
            edges.push(CodewikiGraphEdge::import(
                source_component_id.clone(),
                target_component_id,
            ));
        }
    }
    edges
}

// Both edge queries are project-scoped only. Embedding the core symbol-id or
// file lists in the Cypher text produced payloads in the hundreds of kilobytes
// (every core symbol UUID twice), which intermittently failed at the socket
// layer; core filtering happens client-side in fetch_codewiki_graph_edges.
pub(crate) fn codewiki_call_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target:CodeSymbol {{project: $project}}) \
             RETURN source.id AS source, target.id AS target \
             LIMIT {edge_limit}"
        ),
        HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]),
    )
}

pub(crate) fn codewiki_import_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeFile {{project: $project}})-[:IMPORTS]->(target:CodeModule {{project: $project}}) \
             RETURN source.path AS source, target.name AS target \
             LIMIT {edge_limit}"
        ),
        HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]),
    )
}

#[cfg(test)]
mod graph_timeout_tests {
    use super::{GRAPH_FETCH_TIMEOUT, run_bounded};
    use std::time::Duration;

    #[test]
    fn run_bounded_returns_work_result_when_it_finishes_in_time() {
        let value = run_bounded(Duration::from_secs(5), || 42_u32, || 0_u32);
        assert_eq!(value, 42);
    }

    #[test]
    fn run_bounded_degrades_when_work_never_returns() {
        // A worker that blocks forever stands in for a half-open FalkorDB socket
        // (the redis "unexpected end of file" / mid-restart hang). It must trip
        // the timeout and yield the fallback without blocking the caller.
        let value = run_bounded(
            Duration::from_millis(100),
            || {
                // `_hold` keeps the sender alive so `recv` never observes a
                // disconnect and blocks indefinitely — no wall-clock sleep.
                let (_hold, rx) = std::sync::mpsc::channel::<u8>();
                let _ = rx.recv();
                1_u8
            },
            || 2_u8,
        );
        assert_eq!(value, 2);
    }

    #[test]
    fn graph_fetch_timeout_is_positive() {
        assert!(GRAPH_FETCH_TIMEOUT > Duration::ZERO);
    }
}
