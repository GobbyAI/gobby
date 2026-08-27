use gobby_core::cli_contract::FlagContract;

pub(super) fn format_flag() -> FlagContract {
    FlagContract::value("--format", "json|text").allowed(vec!["json", "text"])
}

pub(super) fn token_budget_flag() -> FlagContract {
    FlagContract::value("--token-budget", "N")
}

pub(super) fn paging_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::value("--limit", "N"),
        FlagContract::value("--offset", "N"),
        token_budget_flag(),
    ]
}

pub(super) fn paged_navigation_flags() -> Vec<FlagContract> {
    paging_flags().into_iter().chain([format_flag()]).collect()
}

pub(super) fn search_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::value("--limit", "N"),
        FlagContract::value("--offset", "N"),
        FlagContract::value("--kind", "KIND"),
        FlagContract::value("--language", "LANG"),
    ]
}

pub(super) fn grep_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::switch("--fixed-strings"),
        FlagContract::switch("--ignore-case"),
        FlagContract::switch("--word"),
        FlagContract::switch("--files-with-matches"),
        FlagContract::switch("--extended-regexp"),
        FlagContract::switch("--line-number"),
        FlagContract::switch("--recursive"),
        FlagContract::switch("-R"),
        FlagContract::value("--before-context", "N"),
        FlagContract::value("--after-context", "N"),
        FlagContract::value("--context", "N"),
        FlagContract::repeatable_value("--glob", "GLOB"),
        FlagContract::value("--limit", "N"),
        FlagContract::value("--offset", "N"),
        token_budget_flag(),
        format_flag(),
    ]
}

pub(super) fn graph_read_flags() -> Vec<FlagContract> {
    paged_navigation_flags()
}

pub(super) fn outline_keys() -> Vec<&'static str> {
    paged_keys(&["id", "name", "kind", "line_start", "line_end", "signature"])
}

pub(super) fn tree_keys() -> Vec<&'static str> {
    paged_keys(&["file_path", "language", "symbol_count"])
}

/// Serialized fields of a stored symbol, shared by `symbol`, `symbol-at`, and
/// `symbols`. Optional `docstring` and `parent_symbol_id` fields are omitted.
pub(super) fn symbol_record_keys() -> Vec<&'static str> {
    vec![
        "id",
        "project_id",
        "file_path",
        "name",
        "qualified_name",
        "kind",
        "language",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
        "signature",
        "content_hash",
        "summary",
        "created_at",
        "updated_at",
    ]
}

pub(super) fn symbol_batch_keys() -> Vec<&'static str> {
    let mut item_keys = symbol_record_keys();
    item_keys.push("source");
    let mut keys = paged_keys(&item_keys);
    keys.push("missing_ids");
    keys
}

pub(super) fn symbol_keys() -> Vec<&'static str> {
    let mut keys = symbol_record_keys();
    keys.push("source");
    keys
}

pub(super) fn symbol_at_keys() -> Vec<&'static str> {
    let mut keys = symbol_keys();
    keys.push("lookup");
    keys
}

pub(super) fn paged_graph_keys() -> Vec<&'static str> {
    paged_keys(&[
        "id",
        "name",
        "file_path",
        "line",
        "confidence",
        "relation",
        "distance",
        "metadata",
    ])
}

pub(super) fn search_keys() -> Vec<&'static str> {
    paged_keys(&[
        "id",
        "name",
        "qualified_name",
        "kind",
        "language",
        "file_path",
        "line_start",
        "line_end",
        "signature",
        "score",
    ])
}

pub(super) fn grep_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "pattern",
        "fixed_strings",
        "ignore_case",
        "word",
        "paths",
        "globs",
        "max_count",
        "matched_lines",
        "truncated",
        "scanned_chunks",
        "offset",
        "next_offset",
        "budget_exceeded",
        "matches",
        "files",
        "path",
        "line",
        "text",
        "spans",
        "start",
        "end",
        "before",
        "after",
    ]
}

pub(super) fn graph_read_keys() -> Vec<&'static str> {
    paged_graph_keys()
}

pub(super) fn graph_path_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "found",
        "from",
        "to",
        "max_depth",
        "hops",
        "path",
        "position",
        "id",
        "display_name",
        "name",
        "file_path",
        "line",
        "hint",
    ]
}

pub(super) fn contract_keys() -> Vec<&'static str> {
    vec![
        "tool",
        "contract_version",
        "summary",
        "global_flags",
        "scope",
        "commands",
        "error_codes",
        "exit_codes",
    ]
}

pub(super) fn graph_payload_keys() -> Vec<&'static str> {
    vec!["nodes", "links", "center"]
}

pub(super) fn graph_view_output_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "project_root",
        "view",
        "seed",
        "depth",
        "incoming_truncated",
        "outgoing_truncated",
        "hint",
        "nodes",
        "edges",
        "communities",
        "mermaid",
    ]
}

pub(super) fn graph_lifecycle_keys() -> Vec<&'static str> {
    vec![
        "status",
        "action",
        "project_id",
        "synced_files",
        "synced_symbols",
        "skipped_files",
        "failed_files",
        "synced_relationships",
        "deleted_nodes",
        "deleted_relationships",
        "summary",
    ]
}

pub(super) fn graph_cleanup_keys() -> Vec<&'static str> {
    vec![
        "status",
        "action",
        "project_id",
        "stale_graph_files_deleted",
        "graph_nodes_deleted",
    ]
}

pub(super) fn graph_report_keys() -> Vec<&'static str> {
    vec!["project_id", "summary", "hotspots", "bridges", "degraded"]
}

pub(super) fn vector_lifecycle_keys() -> Vec<&'static str> {
    vec![
        "success",
        "status",
        "project_id",
        "projection",
        "action",
        "file_path",
        "collection",
        "synced_files",
        "synced_symbols",
        "skipped_files",
        "failed_files",
        "symbols",
        "vectors_upserted",
        "delete_operations_issued",
        "degraded",
        "error",
        "summary",
    ]
}

pub(super) fn vector_cleanup_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "projection",
        "action",
        "collection",
        "status",
        "vector_files_scanned",
        "orphan_files_deleted",
        "vectors_deleted",
        "summary",
    ]
}

pub(super) fn embeddings_doctor_keys() -> Vec<&'static str> {
    vec![
        "endpoint",
        "model",
        "dim",
        "probe_error",
        "peer_error",
        "api_key_present",
        "api_key_fingerprint",
        "namespace_resolved",
        "source",
        "agrees",
        "drift",
    ]
}

pub(super) fn collection_keys() -> Vec<&'static str> {
    paged_keys(&[])
}

pub(super) fn repo_outline_keys() -> Vec<&'static str> {
    paged_keys(&["directory", "file_count", "symbol_count", "files"])
}

fn paged_keys(item_keys: &[&'static str]) -> Vec<&'static str> {
    let mut keys = vec![
        "project_id",
        "total",
        "offset",
        "limit",
        "next_offset",
        "budget_exceeded",
        "results",
    ];
    keys.extend_from_slice(item_keys);
    keys.push("hint");
    keys
}
