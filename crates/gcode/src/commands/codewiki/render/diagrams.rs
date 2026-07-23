use std::collections::VecDeque;
use std::fmt::Write as _;

use gobby_core::vault::mermaid::is_valid_mermaid;

use super::super::*;

const MAX_MERMAID_HOPS: usize = 2;
const MAX_MERMAID_EDGES: usize = 20;
const MAX_CALL_SEQUENCE_PARTICIPANTS: usize = 8;
const MAX_CALL_SEQUENCE_MESSAGES: usize = 12;

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
        "module dependency",
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

pub(crate) fn render_module_call_sequence(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> DiagramOutcome {
    let (component_labels, component_modules) = canonical_component_metadata(files);
    let in_page = |candidate: &str| candidate == module || module_is_ancestor(module, candidate);
    let in_page_components = component_modules
        .iter()
        .filter_map(|(component, owner)| in_page(owner).then_some(component.clone()))
        .collect::<BTreeSet<_>>();
    if in_page_components.is_empty() {
        return DiagramOutcome::SparseEvidence;
    }

    // Canonicalize before seed selection, traversal, and capping because graph
    // query order is unstable across equivalent FalkorDB result sets.
    let all_edges = graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Call)
        .filter_map(|edge| {
            let source_module = component_modules.get(&edge.source_component_id)?;
            let target_module = component_modules.get(&edge.target_component_id)?;
            (in_page(source_module) || in_page(target_module)).then(|| {
                (
                    edge.source_component_id.clone(),
                    edge.target_component_id.clone(),
                )
            })
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return DiagramOutcome::SparseEvidence;
    }

    let incoming_from_page = all_edges
        .iter()
        .filter(|(source, target)| {
            in_page_components.contains(source) && in_page_components.contains(target)
        })
        .map(|(_, target)| target.clone())
        .collect::<BTreeSet<_>>();
    let root_seeds = in_page_components
        .difference(&incoming_from_page)
        .cloned()
        .collect::<BTreeSet<_>>();
    let distances = if root_seeds.is_empty() {
        // Cyclic pages have no roots. Try every component as a deterministic
        // fallback seed and keep the first traversal that proves two levels.
        in_page_components.iter().find_map(|seed| {
            let seeds = BTreeSet::from([seed.clone()]);
            let distances = directed_call_distances(&seeds, &all_edges, MAX_MERMAID_HOPS);
            distances
                .values()
                .any(|distance| *distance >= 2)
                .then_some(distances)
        })
    } else {
        Some(directed_call_distances(
            &root_seeds,
            &all_edges,
            MAX_MERMAID_HOPS,
        ))
    };
    let Some(distances) = distances else {
        return DiagramOutcome::SparseEvidence;
    };
    if !distances.values().any(|distance| *distance >= 2) {
        return DiagramOutcome::SparseEvidence;
    }

    let mut ranked_edges = all_edges
        .iter()
        .filter_map(|(source, target)| {
            let source_depth = *distances.get(source)?;
            let target_depth = *distances.get(target)?;
            (source_depth < MAX_MERMAID_HOPS
                && target_depth <= MAX_MERMAID_HOPS
                && target_depth <= source_depth + 1)
                .then(|| (source_depth, target_depth, source.clone(), target.clone()))
        })
        .collect::<Vec<_>>();
    ranked_edges.sort();

    // Reserve one canonical depth-two witness before filling the remaining
    // participant/message budget, so a wide first level cannot erase the very
    // chain that makes this slot eligible.
    let Some((_, _, witness_source, witness_target)) = ranked_edges
        .iter()
        .find(|(source_depth, target_depth, _, _)| *source_depth == 1 && *target_depth == 2)
        .cloned()
    else {
        return DiagramOutcome::SparseEvidence;
    };
    let Some((_, _, witness_root, _)) = ranked_edges
        .iter()
        .find(|(source_depth, target_depth, _, target)| {
            *source_depth == 0 && *target_depth == 1 && target == &witness_source
        })
        .cloned()
    else {
        return DiagramOutcome::SparseEvidence;
    };

    let mut bounded_edges = BTreeSet::from([
        (witness_root, witness_source.clone()),
        (witness_source, witness_target),
    ]);
    let mut participants = bounded_edges
        .iter()
        .flat_map(|(source, target)| [source.clone(), target.clone()])
        .collect::<BTreeSet<_>>();
    for (_, _, source, target) in &ranked_edges {
        if bounded_edges.len() >= MAX_CALL_SEQUENCE_MESSAGES {
            break;
        }
        if bounded_edges.contains(&(source.clone(), target.clone())) {
            continue;
        }
        let added_participants = usize::from(!participants.contains(source))
            + usize::from(!participants.contains(target));
        if participants.len() + added_participants > MAX_CALL_SEQUENCE_PARTICIPANTS {
            continue;
        }
        participants.insert(source.clone());
        participants.insert(target.clone());
        bounded_edges.insert((source.clone(), target.clone()));
    }

    let mut ordered_participants = participants.into_iter().collect::<Vec<_>>();
    ordered_participants.sort_by_key(|component| {
        (
            distances.get(component).copied().unwrap_or(usize::MAX),
            component.clone(),
        )
    });
    let mut ordered_edges = bounded_edges.into_iter().collect::<Vec<_>>();
    ordered_edges.sort_by_key(|(source, target)| {
        (
            distances.get(source).copied().unwrap_or(usize::MAX),
            distances.get(target).copied().unwrap_or(usize::MAX),
            source.clone(),
            target.clone(),
        )
    });

    let mut fence = "```mermaid\nsequenceDiagram\n".to_string();
    for component in ordered_participants {
        let label = component_labels
            .get(&component)
            .map(String::as_str)
            .unwrap_or(&component);
        let _ = writeln!(
            fence,
            "    participant {} as {}",
            mermaid_node_id(&component),
            mermaid_label(label)
        );
    }
    let shown_edges = ordered_edges.len();
    for (source, target) in ordered_edges {
        let _ = writeln!(
            fence,
            "    {}->>{}: calls",
            mermaid_node_id(&source),
            mermaid_node_id(&target)
        );
    }
    fence.push_str("```\n");
    if !is_valid_mermaid(&fence) {
        return DiagramOutcome::Rejected;
    }

    let mut diagram = simplified_diagram_note(
        "symbol call",
        shown_edges,
        all_edges.len(),
        graph_availability == CodewikiGraphAvailability::Truncated,
    );
    diagram.push_str(
        "_Static call sequence — indexed call edges ordered by call depth; not a recorded execution trace._\n\n",
    );
    diagram.push_str(&fence);
    DiagramOutcome::Emitted(diagram)
}

fn canonical_component_metadata(
    files: &[FileDoc],
) -> (BTreeMap<String, String>, BTreeMap<String, String>) {
    let mut labels = BTreeMap::new();
    let mut modules = BTreeMap::new();
    for file in files {
        for component in &file.component_ids {
            modules
                .entry(component.clone())
                .and_modify(|current: &mut String| {
                    if file.module < *current {
                        current.clone_from(&file.module);
                    }
                })
                .or_insert_with(|| file.module.clone());
        }
        for symbol in &file.symbols {
            labels
                .entry(symbol.component_id.clone())
                .and_modify(|current: &mut String| {
                    if symbol.component_label < *current {
                        current.clone_from(&symbol.component_label);
                    }
                })
                .or_insert_with(|| symbol.component_label.clone());
        }
    }
    (labels, modules)
}

fn directed_call_distances(
    seeds: &BTreeSet<String>,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
) -> BTreeMap<String, usize> {
    let mut distances = seeds
        .iter()
        .map(|seed| (seed.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut queue = seeds
        .iter()
        .map(|seed| (seed.clone(), 0usize))
        .collect::<VecDeque<_>>();
    while let Some((current, depth)) = queue.pop_front() {
        if depth >= max_hops {
            continue;
        }
        for (_, target) in edges.iter().filter(|(source, _)| source == &current) {
            if distances.contains_key(target) {
                continue;
            }
            let target_depth = depth + 1;
            distances.insert(target.clone(), target_depth);
            queue.push_back((target.clone(), target_depth));
        }
    }
    distances
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
    edge_kind: &str,
    shown_edges: usize,
    total_edges: usize,
    graph_truncated: bool,
) -> String {
    if graph_truncated {
        return format!(
            "_Simplified diagram: showing top {shown_edges} of {total_edges} available {edge_kind} edge(s); source graph was truncated._\n\n"
        );
    }
    if shown_edges < total_edges {
        return format!(
            "_Simplified diagram: showing top {shown_edges} of {total_edges} {edge_kind} edge(s) within diagram bounds._\n\n"
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
