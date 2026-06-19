use super::super::*;

/// Import edges between distinct subsystem roots, attributed through each
/// component's owning file. This is graph-derived ANALYSIS that feeds the
/// architecture narrative prose (the cross-subsystem dependency edges in the
/// narrative prompt); it no longer renders a diagram.
pub(crate) fn collect_subsystem_dependency_edges(
    roots: &BTreeSet<String>,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> BTreeSet<(String, String)> {
    let mut component_to_root = HashMap::new();
    for file in files {
        let Some(root) = cluster::subsystem_root_for_file(&file.path, roots) else {
            continue;
        };
        for component_id in &file.component_ids {
            component_to_root.insert(component_id.as_str(), root);
        }
    }

    graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Import)
        .filter_map(|edge| {
            let source = component_to_root.get(edge.source_component_id.as_str())?;
            let target = component_to_root.get(edge.target_component_id.as_str())?;
            if source == target {
                return None;
            }
            Some(((*source).to_string(), (*target).to_string()))
        })
        .collect()
}
