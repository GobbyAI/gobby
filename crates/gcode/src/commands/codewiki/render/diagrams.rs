use std::collections::VecDeque;
use std::fmt::Write as _;

use gobby_core::vault::mermaid::is_valid_mermaid;

use super::super::*;

const MAX_MERMAID_HOPS: usize = 2;
const MAX_MERMAID_EDGES: usize = 20;

pub(crate) fn render_module_dependency_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> DiagramOutcome {
    // Canonicalize and deduplicate before aggregation, traversal, and capping.
    // FalkorDB result order is unstable; equivalent fact sets must still emit
    // byte-identical diagrams and captions.
    let all_edges = collect_module_dependency_edges(files, graph_edges)
        .into_iter()
        .filter_map(|(source, target)| {
            let source = aggregate_module_for_page(module, &source);
            let target = aggregate_module_for_page(module, &target);
            (source != target).then_some((source, target))
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return DiagramOutcome::SparseEvidence;
    }

    let mut bounded_edges =
        bounded_module_dependency_edges(module, &all_edges, MAX_MERMAID_HOPS, MAX_MERMAID_EDGES);
    if bounded_edges.is_empty() {
        // Container pages may have no edge incident on the container node
        // after aggregation. Their child-to-child edges still describe the
        // module's internal dependency structure.
        let page_prefix = format!("{module}/");
        bounded_edges = all_edges
            .iter()
            .filter(|(source, target)| {
                source.starts_with(&page_prefix) && target.starts_with(&page_prefix)
            })
            .take(MAX_MERMAID_EDGES)
            .cloned()
            .collect();
    }
    if bounded_edges.is_empty() {
        return DiagramOutcome::SparseEvidence;
    }

    let shown_edges = bounded_edges.len();
    let omitted_edges = all_edges.len().saturating_sub(bounded_edges.len());
    let mut fence = "```mermaid\ngraph LR\n".to_string();
    if omitted_edges > 0 {
        let _ = writeln!(
            fence,
            "    %% Partial module dependency graph: {omitted_edges} edge(s) omitted by bounds"
        );
    }
    for (source, target) in bounded_edges {
        let _ = writeln!(
            fence,
            "    {}[\"{}\"] --> {}[\"{}\"]",
            mermaid_node_id(&source),
            mermaid_label(&source),
            mermaid_node_id(&target),
            mermaid_label(&target)
        );
    }
    fence.push_str("```\n");
    if !is_valid_mermaid(&fence) {
        return DiagramOutcome::Rejected;
    }

    let mut diagram = simplified_diagram_note(
        shown_edges,
        all_edges.len(),
        graph_availability == CodewikiGraphAvailability::Truncated,
    );
    diagram.push_str(&fence);
    DiagramOutcome::Emitted(diagram)
}

fn collect_module_dependency_edges(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> BTreeSet<(String, String)> {
    let component_to_module = files
        .iter()
        .flat_map(|file| {
            file.component_ids
                .iter()
                .map(|component_id| (component_id.as_str(), file.module.as_str()))
        })
        .collect::<HashMap<_, _>>();

    graph_edges
        .iter()
        .filter(|edge| {
            matches!(
                edge.kind,
                CodewikiGraphEdgeKind::Import | CodewikiGraphEdgeKind::Call
            )
        })
        .filter_map(|edge| {
            let source = component_to_module.get(edge.source_component_id.as_str())?;
            let target = component_to_module.get(edge.target_component_id.as_str())?;
            (source != target).then(|| ((*source).to_string(), (*target).to_string()))
        })
        .collect()
}

/// Maps an endpoint to the page itself, one direct child, or an external
/// module truncated to the page depth so sibling nodes remain comparable.
pub(crate) fn aggregate_module_for_page(page: &str, module: &str) -> String {
    if module == page {
        return page.to_string();
    }
    if let Some(rest) = module.strip_prefix(page)
        && let Some(rest) = rest.strip_prefix('/')
    {
        let child = rest.split('/').next().unwrap_or_default();
        if !child.is_empty() {
            return format!("{page}/{child}");
        }
        return page.to_string();
    }
    let depth = module_depth(page).max(1);
    module
        .split('/')
        .filter(|part| !part.is_empty())
        .take(depth)
        .collect::<Vec<_>>()
        .join("/")
}

pub(crate) fn bounded_module_dependency_edges(
    module: &str,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
    max_edges: usize,
) -> Vec<(String, String)> {
    let mut distances = BTreeMap::from([(module.to_string(), 0usize)]);
    let mut queue = VecDeque::from([(module.to_string(), 0usize)]);

    while let Some((current, distance)) = queue.pop_front() {
        if distance >= max_hops {
            continue;
        }
        for (source, target) in edges {
            for next in dependency_neighbors(&current, source, target) {
                if distances.contains_key(next) {
                    continue;
                }
                let next_distance = distance + 1;
                distances.insert(next.to_string(), next_distance);
                queue.push_back((next.to_string(), next_distance));
            }
        }
    }

    let mut reachable = edges
        .iter()
        .filter(|(source, target)| distances.contains_key(source) && distances.contains_key(target))
        .map(|(source, target)| {
            (
                distances[source].max(distances[target]),
                source.clone(),
                target.clone(),
            )
        })
        .collect::<Vec<_>>();
    reachable.sort();
    let mut bounded = reachable
        .into_iter()
        .take(max_edges)
        .map(|(_, source, target)| (source, target))
        .collect::<Vec<_>>();
    bounded.sort();
    bounded
}

fn dependency_neighbors<'a>(module: &str, source: &'a str, target: &'a str) -> Vec<&'a str> {
    let mut neighbors = Vec::with_capacity(2);
    if source == module {
        neighbors.push(target);
    }
    if target == module {
        neighbors.push(source);
    }
    neighbors
}

fn simplified_diagram_note(
    shown_edges: usize,
    total_edges: usize,
    graph_truncated: bool,
) -> String {
    if graph_truncated {
        return format!(
            "_Simplified diagram: showing top {shown_edges} of {total_edges} available module dependency edge(s); source graph was truncated._\n\n"
        );
    }
    if shown_edges < total_edges {
        return format!(
            "_Simplified diagram: showing top {shown_edges} of {total_edges} module dependency edge(s) within diagram bounds._\n\n"
        );
    }
    String::new()
}

fn mermaid_node_id(module: &str) -> String {
    let mut out = String::from("m_");
    for ch in module.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    out
}

fn mermaid_label(module: &str) -> String {
    if module.is_empty() {
        "repo".to_string()
    } else {
        module
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('[', "&#91;")
            .replace(']', "&#93;")
            .replace('(', "&#40;")
            .replace(')', "&#41;")
            .replace('{', "&#123;")
            .replace('}', "&#125;")
            .replace('|', "&#124;")
    }
}

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
