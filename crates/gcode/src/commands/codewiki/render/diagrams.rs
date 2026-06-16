use std::fmt::Write as _;

use super::super::*;

pub(crate) fn render_module_dependency_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    // Aggregate edge endpoints relative to this page (the page itself, one
    // of its direct children, or an external module truncated to the page's
    // depth) so dependency diagrams render at every module level that has
    // bounded edges, not only at leaf cluster granularity.
    let all_edges = collect_import_module_edges(files, graph_edges)
        .into_iter()
        .filter_map(|(source, target)| {
            let source = aggregate_module_for_page(module, &source);
            let target = aggregate_module_for_page(module, &target);
            (source != target).then_some((source, target))
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return None;
    }

    let mut bounded_edges = bounded_module_dependency_edges(module, &all_edges, MAX_MERMAID_HOPS);
    if bounded_edges.is_empty() {
        // The page module itself has no direct edges; fall back to edges
        // among its aggregated children so container modules still render
        // their internal dependency structure.
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
        return None;
    }

    let omitted_edges = all_edges.len().saturating_sub(bounded_edges.len());
    let mut diagram = "```mermaid\ngraph LR\n".to_string();
    write_partial_import_graph_comment(&mut diagram, omitted_edges);
    for (source, target) in bounded_edges {
        let _ = writeln!(
            diagram,
            "    {}[\"{}\"] --> {}[\"{}\"]",
            mermaid_node_id(&source),
            mermaid_label(&source),
            mermaid_node_id(&target),
            mermaid_label(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

/// Maps an edge-endpoint module to a node on `page`'s dependency diagram:
/// the page itself, `page/<child>` for descendants, or an external module
/// truncated to the page's depth so siblings stay comparable.
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

/// Crate/subsystem-level dependency diagram: import edges aggregated to the
/// supplied subsystem roots. Returns `None` when no cross-root edges exist —
/// an edge-free diagram is never fabricated.
pub(crate) fn render_subsystem_dependency_mermaid(
    roots: &BTreeSet<String>,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let edges = collect_subsystem_dependency_edges(roots, files, graph_edges);
    if edges.is_empty() {
        return None;
    }

    let bounded_edges = edges
        .iter()
        .take(MAX_MERMAID_EDGES)
        .cloned()
        .collect::<Vec<_>>();
    let omitted_edges = edges.len().saturating_sub(bounded_edges.len());
    let mut diagram = "```mermaid\ngraph LR\n".to_string();
    write_partial_import_graph_comment(&mut diagram, omitted_edges);
    for (source, target) in bounded_edges {
        let _ = writeln!(
            diagram,
            "    {}[\"{}\"] --> {}[\"{}\"]",
            mermaid_node_id(&source),
            mermaid_label(&source),
            mermaid_node_id(&target),
            mermaid_label(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

/// Structural fallback for the architecture overview when graph-derived
/// subsystem dependencies are unavailable or edge-free.
pub(crate) fn render_architecture_structure_mermaid(
    subsystems: &[ArchitectureSubsystem],
) -> Option<String> {
    if subsystems.is_empty() {
        return None;
    }

    let mut links = Vec::new();
    for subsystem in subsystems {
        links.push(("".to_string(), subsystem.module.clone()));
        for child in &subsystem.child_modules {
            links.push((subsystem.module.clone(), child.clone()));
        }
    }

    let omitted_links = links.len().saturating_sub(MAX_MERMAID_EDGES);
    let mut diagram = "```mermaid\ngraph TD\n".to_string();
    if omitted_links > 0 {
        let _ = writeln!(
            diagram,
            "    %% {omitted_links} additional subsystem links omitted"
        );
    }
    for (source, target) in links.into_iter().take(MAX_MERMAID_EDGES) {
        let _ = writeln!(
            diagram,
            "    {}[\"{}\"] --> {}[\"{}\"]",
            mermaid_node_id(&source),
            mermaid_label(&source),
            mermaid_node_id(&target),
            mermaid_label(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

/// Import edges between distinct subsystem roots, attributed through each
/// component's owning file.
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

fn collect_import_module_edges(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> BTreeSet<(String, String)> {
    let mut component_to_module = HashMap::new();
    for file in files {
        for component_id in &file.component_ids {
            component_to_module.insert(component_id.as_str(), file.module.as_str());
        }
    }

    graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Import)
        .filter_map(|edge| {
            let source = component_to_module.get(edge.source_component_id.as_str())?;
            let target = component_to_module.get(edge.target_component_id.as_str())?;
            if source == target {
                return None;
            }
            Some(((*source).to_string(), (*target).to_string()))
        })
        .collect()
}

fn write_partial_import_graph_comment(diagram: &mut String, omitted_edges: usize) {
    if omitted_edges > 0 {
        let _ = writeln!(
            diagram,
            "    %% Partial import graph: {omitted_edges} edge(s) omitted by bounds"
        );
    }
}

pub(crate) fn render_module_call_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let component_labels = files
        .iter()
        .flat_map(|file| {
            file.symbols.iter().map(|symbol| {
                (
                    symbol.component_id.as_str(),
                    symbol.component_label.as_str(),
                )
            })
        })
        .collect::<HashMap<_, _>>();
    let component_to_module = files
        .iter()
        .flat_map(|file| {
            file.component_ids
                .iter()
                .map(|component_id| (component_id.as_str(), file.module.as_str()))
        })
        .collect::<HashMap<_, _>>();
    let all_edges = graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Call)
        .filter_map(|edge| {
            let source_module = component_to_module.get(edge.source_component_id.as_str())?;
            let target_module = component_to_module.get(edge.target_component_id.as_str())?;
            let in_page =
                |candidate: &str| candidate == module || module_is_ancestor(module, candidate);
            if !in_page(source_module) && !in_page(target_module) {
                return None;
            }
            Some((
                edge.source_component_id.clone(),
                edge.target_component_id.clone(),
            ))
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return None;
    }

    let seed_components = files
        .iter()
        .filter(|file| file.module == module || module_is_ancestor(module, &file.module))
        .flat_map(|file| file.component_ids.iter().cloned())
        .collect::<BTreeSet<_>>();
    let bounded_edges = bounded_component_edges(
        &seed_components,
        &all_edges,
        MAX_MERMAID_HOPS,
        MAX_MERMAID_EDGES,
    );
    if bounded_edges.is_empty() {
        return None;
    }

    let mut participants = BTreeSet::new();
    for (source, target) in &bounded_edges {
        participants.insert(source.clone());
        participants.insert(target.clone());
    }

    let mut diagram = "```mermaid\nsequenceDiagram\n".to_string();
    for component in participants {
        let _ = writeln!(
            diagram,
            "    participant {} as {}",
            mermaid_node_id(&component),
            mermaid_label(
                component_labels
                    .get(component.as_str())
                    .copied()
                    .unwrap_or(&component)
            )
        );
    }
    for (source, target) in bounded_edges {
        let _ = writeln!(
            diagram,
            "    {}->>{}: calls",
            mermaid_node_id(&source),
            mermaid_node_id(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

pub(crate) fn bounded_module_dependency_edges(
    module: &str,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
) -> BTreeSet<(String, String)> {
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

    edges
        .iter()
        .filter(|(source, target)| distances.contains_key(source) && distances.contains_key(target))
        .cloned()
        .collect()
}

pub(crate) fn bounded_component_edges(
    seed_components: &BTreeSet<String>,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
    max_edges: usize,
) -> BTreeSet<(String, String)> {
    let mut distances = seed_components
        .iter()
        .map(|component| (component.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut queue = seed_components
        .iter()
        .map(|component| (component.clone(), 0usize))
        .collect::<VecDeque<_>>();

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

    let mut reachable_edges = edges
        .iter()
        .filter(|(source, target)| distances.contains_key(source) && distances.contains_key(target))
        .map(|(source, target)| {
            let source_distance = distances[source];
            let target_distance = distances[target];
            (
                source_distance.max(target_distance),
                source.clone(),
                target.clone(),
            )
        })
        .collect::<Vec<_>>();
    reachable_edges.sort();
    reachable_edges
        .into_iter()
        .take(max_edges)
        .map(|(_, source, target)| (source, target))
        .collect()
}

pub(crate) fn dependency_neighbors<'a>(
    module: &str,
    source: &'a str,
    target: &'a str,
) -> Vec<&'a str> {
    let mut neighbors = Vec::with_capacity(2);
    if source == module {
        neighbors.push(target);
    }
    if target == module {
        neighbors.push(source);
    }
    neighbors
}

pub(crate) fn mermaid_node_id(module: &str) -> String {
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

pub(crate) fn mermaid_label(module: &str) -> String {
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
