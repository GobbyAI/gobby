use std::fmt::Write as _;

use super::*;

pub(crate) fn render_module_dependency_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let all_edges = collect_module_edges(files, graph_edges);
    if all_edges.is_empty() {
        return None;
    }

    let bounded_edges = bounded_module_dependency_edges(module, &all_edges, MAX_MERMAID_HOPS);
    if bounded_edges.is_empty() {
        return None;
    }

    let mut diagram = "```mermaid\ngraph LR\n".to_string();
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

pub(crate) fn render_architecture_dependency_mermaid(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let edges = collect_module_edges(files, graph_edges);
    if edges.is_empty() {
        return None;
    }

    let mut diagram = "```mermaid\ngraph LR\n".to_string();
    for (source, target) in edges {
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

fn collect_module_edges(
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
            if *source_module != module && *target_module != module {
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

pub(crate) fn build_repo_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> String {
    let top_modules = modules
        .iter()
        .filter(|module| parent_module(&module.module).is_none())
        .map(|module| ModuleLink {
            module: module.module.clone(),
            summary: module.summary.clone(),
            source_spans: module.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let root_files = files
        .iter()
        .filter(|file| file.module.is_empty())
        .map(|file| FileLink {
            path: file.path.clone(),
            summary: file.summary.clone(),
            source_spans: file.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let module_summaries = top_modules
        .iter()
        .map(|module| prompts::ChildSummary {
            name: module.module.clone(),
            summary: module.summary.clone(),
        })
        .collect::<Vec<_>>();
    let file_summaries = root_files
        .iter()
        .map(|file| prompts::ChildSummary {
            name: file.path.clone(),
            summary: file.summary.clone(),
        })
        .collect::<Vec<_>>();
    let fallback = structural_repo_summary(files.len(), modules.len());
    let source_spans = collect_link_spans(&root_files, &top_modules);
    let generated = maybe_generate(
        generate,
        &prompts::repo_prompt(&module_summaries, &file_summaries),
        prompts::REPO_SYSTEM,
    )
    .unwrap_or(fallback);
    let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

    render_repo_doc(&summary, &top_modules, &root_files, &source_spans)
}

pub(crate) fn render_repo_doc(
    summary: &str,
    modules: &[ModuleLink],
    files: &[FileLink],
    source_spans: &[SourceSpan],
) -> String {
    let mut doc = frontmatter("Repository Overview", "code_repo", source_spans);
    doc.push_str("# Repository Overview\n\n");
    write_section(&mut doc, "Overview", summary);
    if !modules.is_empty() {
        doc.push_str("## Modules\n\n");
        for module in modules {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&module.module),
                module.summary
            );
        }
        doc.push('\n');
    }
    if !files.is_empty() {
        doc.push_str("## Files\n\n");
        for file in files {
            let _ = writeln!(doc, "- {} - {}", file_wikilink(&file.path), file.summary);
        }
        doc.push('\n');
    }
    doc
}

pub(crate) fn render_architecture_doc(architecture: &ArchitectureDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        "Architecture Overview",
        "code_architecture",
        &architecture.source_spans,
        &architecture.degraded_sources,
    );
    doc.push_str("# Architecture Overview\n\n");
    if let Some(diagram) = &architecture.dependency_diagram {
        doc.push_str("## Subsystem Map\n\n");
        doc.push_str(diagram);
        doc.push('\n');
    }
    if !architecture.subsystems.is_empty() {
        doc.push_str("## Subsystems\n\n");
        for subsystem in &architecture.subsystems {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&subsystem.module),
                subsystem.responsibility
            );
        }
        doc.push('\n');
    }
    doc
}

pub(crate) fn render_onboarding_doc(onboarding: &OnboardingDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        "Start Here",
        "code_onboarding",
        &onboarding.source_spans,
        &onboarding.degraded_sources,
    );
    doc.push_str("# Start Here\n\n");

    if !onboarding.entry_points.is_empty() {
        doc.push_str("## Entry Points\n\n");
        for entry in &onboarding.entry_points {
            let _ = writeln!(doc, "- {} - {}", entry.link, entry.description);
        }
        doc.push('\n');
    }

    if onboarding.reading_order.is_empty() {
        if !onboarding.entry_points.is_empty() {
            doc.push_str("## Structural Start Points\n\n");
            for entry in &onboarding.entry_points {
                let _ = writeln!(doc, "- {} - {}", entry.link, entry.description);
            }
            doc.push('\n');
        }
        return doc;
    }

    doc.push_str("## Recommended Reading Order\n\n");
    for (index, step) in onboarding.reading_order.iter().enumerate() {
        let _ = writeln!(
            doc,
            "{}. {} - {} centrality degree, {:.3} score. {}",
            index + 1,
            module_wikilink(&step.module),
            step.degree,
            step.score,
            step.summary
        );
    }
    doc.push('\n');
    doc
}

pub(crate) fn render_hotspots_doc(hotspots: &HotspotsDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        "Hotspots",
        "code_hotspots",
        &hotspots.source_spans,
        &hotspots.degraded_sources,
    );
    doc.push_str("# Hotspots\n\n");

    if hotspots
        .degraded_sources
        .iter()
        .any(|source| source == "graph-analytics-unavailable")
    {
        doc.push_str("analytics unavailable: graph analytics could not be computed.\n\n");
        return doc;
    }

    write_hotspot_section(&mut doc, "Hotspots", &hotspots.hotspots);
    write_hotspot_section(&mut doc, "God Nodes", &hotspots.god_nodes);
    write_hotspot_section(&mut doc, "Bridges", &hotspots.bridges);

    if hotspots.hotspots.is_empty() && hotspots.god_nodes.is_empty() && hotspots.bridges.is_empty()
    {
        doc.push_str("No graph hotspots were identified from the current code index.\n\n");
    }

    doc
}

fn write_hotspot_section(doc: &mut String, title: &str, findings: &[HotspotFinding]) {
    doc.push_str("## ");
    doc.push_str(title);
    doc.push_str("\n\n");

    if findings.is_empty() {
        doc.push_str("None identified.\n\n");
        return;
    }

    for finding in findings {
        let mut details = Vec::new();
        details.push(format!("kind {}", inline_code(&finding.node.kind)));
        details.push(format!("component {}", inline_code(&finding.node.id)));
        if let Some(degree) = finding.degree {
            details.push(format!("degree {degree}"));
        }
        if let Some(score) = finding.score {
            details.push(format!("score {score:.3}"));
        }
        if let Some(frequency) = finding.frequency {
            details.push(format!("frequency {frequency}"));
        }
        if let Some(weight) = finding.weight {
            details.push(format!("weight {weight:.1}"));
        }
        if let Some(file) = &finding.node.file_wikilink {
            details.push(format!("file {file}"));
        }
        if let Some(span) = &finding.node.source_span {
            details.push(span.citation());
        }

        let _ = writeln!(
            doc,
            "- {} ({}) - {}",
            finding.node.wikilink,
            inline_code(&finding.node.label),
            details.join(", ")
        );
    }
    doc.push('\n');
}

pub(crate) fn render_module_doc(module: &ModuleDoc) -> String {
    let mut doc = frontmatter(&module.module, "code_module", &module.source_spans);
    let _ = writeln!(doc, "# {}\n", module.module);
    match parent_module(&module.module) {
        Some(parent) => {
            let _ = writeln!(doc, "Parent: {}\n", module_wikilink(parent));
        }
        None => doc.push_str("Parent: [[code/repo|Repository Overview]]\n\n"),
    }
    write_section(&mut doc, "Overview", &module.summary);
    match module.graph_availability {
        CodewikiGraphAvailability::Unavailable => {
            doc.push_str("## Dependency Diagram\n\n`degraded: graph-unavailable`\n\n");
        }
        CodewikiGraphAvailability::Available | CodewikiGraphAvailability::Truncated => {
            if module.graph_availability == CodewikiGraphAvailability::Truncated {
                doc.push_str("## Dependency Diagram\n\n`degraded: graph-truncated`\n\n");
            }
            if let Some(diagram) = &module.dependency_diagram {
                if module.graph_availability == CodewikiGraphAvailability::Available {
                    doc.push_str("## Dependency Diagram\n\n");
                }
                doc.push_str(diagram);
                doc.push('\n');
            }
            if let Some(diagram) = &module.call_diagram {
                doc.push_str("## Call Diagram\n\n");
                doc.push_str(diagram);
                doc.push('\n');
            }
        }
    }
    if !module.child_modules.is_empty() {
        doc.push_str("## Child Modules\n\n");
        for child in &module.child_modules {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&child.module),
                child.summary
            );
        }
        doc.push('\n');
    }
    if !module.direct_files.is_empty() {
        doc.push_str("## Files\n\n");
        for file in &module.direct_files {
            let _ = writeln!(doc, "- {} - {}", file_wikilink(&file.path), file.summary);
        }
        doc.push('\n');
    }
    if !module.component_ids.is_empty() {
        doc.push_str("## Components\n\n");
        for component_id in &module.component_ids {
            let _ = writeln!(doc, "- {}", inline_code(component_id));
        }
        doc.push('\n');
    }
    doc
}

pub(crate) fn render_file_doc(file: &FileDoc) -> String {
    let mut doc = frontmatter(&file.path, "code_file", &file.source_spans);
    let _ = writeln!(doc, "# {}\n", file.path);
    if file.module.is_empty() {
        doc.push_str("Module: [[code/repo|Repository Overview]]\n\n");
    } else {
        let _ = writeln!(doc, "Module: {}\n", module_wikilink(&file.module));
    }
    write_section(&mut doc, "Purpose", &file.summary);
    doc.push_str("## API Symbols\n\n");
    if file.symbols.is_empty() {
        doc.push_str("No indexed symbols.\n");
        return doc;
    }
    for symbol in &file.symbols {
        let _ = writeln!(
            doc,
            "- {} ({}) component {} ({}) lines {}-{} {}",
            inline_code(&symbol.symbol.qualified_name),
            symbol.symbol.kind,
            inline_code(&symbol.component_label),
            inline_code(&symbol.component_id),
            symbol.symbol.line_start,
            symbol.symbol.line_end,
            symbol.source_span.citation()
        );
        if let Some(signature) = symbol
            .symbol
            .signature
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let _ = writeln!(doc, "  - Signature: {}", inline_code(signature));
        }
        let _ = writeln!(doc, "  - Purpose: {}", symbol.purpose);
    }
    doc.push('\n');
    doc
}
