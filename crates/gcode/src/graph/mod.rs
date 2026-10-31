pub mod code_graph;
pub mod report;
pub mod typed_query;

/// Admit every CodeSymbol by its exact file version. A symbol with its own file
/// props is checked against its namespace. A callee placeholder carries only the
/// version stamped from its symbol row, checked against the scope endpoint set.
/// Other node classes derive visibility from the separately admitted relationship.
pub(crate) fn admitted_code_nodes(nodes: &str) -> String {
    format!(
        "all(node IN {nodes} WHERE (NOT node:CodeSymbol OR \
         coalesce((node.file_path + '|' + node.file_content_hash) IN $admitted_versions, false) OR \
         coalesce((node.version_file_path + '|' + node.version_content_hash) IN $admitted_endpoints, false)))"
    )
}

#[cfg(test)]
mod tests;
