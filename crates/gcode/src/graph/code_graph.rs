mod connection;
mod lifecycle;
mod payload;
mod read;
#[cfg(test)]
mod tests;
mod write;

pub use connection::require_graph_reads;
pub use lifecycle::{
    GraphLifecycleAction, GraphLifecycleOutput, GraphLifecycleRequest, GraphReadError,
    GraphReadRequest, require_daemon_url, run_lifecycle_action,
};
pub use payload::{
    GraphBlastRadiusTarget, GraphLink, GraphNode, GraphPayload, extracted_code_edge_metadata,
};
pub use read::{
    DEFAULT_SYMBOL_PATH_MAX_DEPTH, MAX_SYMBOL_PATH_DEPTH, ResolvedExternalCallTarget, blast_radius,
    blast_radius_graph, count_callers, count_usages, file_graph, find_callee_ids_batch,
    find_callees_batch, find_caller_ids, find_caller_ids_batch, find_callers, find_callers_batch,
    find_usage_ids, find_usages, get_imports, project_overview_graph, resolve_external_call_target,
    shortest_symbol_path, symbol_neighbors,
};
pub use write::{
    CodeGraph, GraphOrphanCleanup, call_target_id, cleanup_deleted_files, cleanup_orphans,
    clear_all_code_index, clear_project, delete_file_graph, delete_file_projection,
    sync_file_graph, with_code_graph,
};

pub(crate) use lifecycle::extract_summary_text;
#[cfg(test)]
pub(crate) use read::{
    get_imports_query, resolve_external_call_target_query, symbol_callee_edges_query,
    symbol_path_steps_query,
};

#[cfg(test)]
pub(crate) use lifecycle::{
    build_lifecycle_url, compact_detail, format_http_error, parse_success_payload,
};
#[cfg(test)]
use payload::{row_string_owned, row_to_projection_metadata, row_usize};
#[cfg(test)]
use read::{blast_radius_file_import_query, dedupe_limited_blast_rows, file_calls_query};
#[cfg(test)]
use write::{
    cleanup_orphans_queries, clear_all_code_index_query, clear_project_query,
    count_file_projection_nodes_query, delete_file_graph_queries, delete_file_node_query,
    import_graph_items, partition_call_graph_items, project_file_path_queries,
};
