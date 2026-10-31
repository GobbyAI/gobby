pub mod code_graph;
pub mod report;
pub mod typed_query;

/// Admit every CodeSymbol by its exact file version. Other node classes derive
/// visibility from the separately admitted relationship, not a symbol version.
pub(crate) fn admitted_code_nodes(nodes: &str) -> String {
    format!(
        "all(node IN {nodes} WHERE (NOT node:CodeSymbol OR \
         coalesce(node.file_path + '|' + node.file_content_hash IN $admitted_versions, false)))"
    )
}
