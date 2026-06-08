use super::*;
use gobby_core::graph_analytics::{self, AnalyticsEdge, AnalyticsGraph, AnalyticsNode};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub(crate) fn build_codewiki_index_snapshot(
    project_root: &Path,
    input: &CodewikiInput,
) -> anyhow::Result<CodewikiIndexSnapshot> {
    let mut files = input
        .files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<BTreeSet<_>>();
    for symbol in &input.symbols {
        if is_core_file(&symbol.file_path) {
            files.insert(symbol.file_path.clone());
        }
    }

    let symbols_by_file = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .fold(BTreeMap::<String, usize>::new(), |mut counts, symbol| {
            *counts.entry(symbol.file_path.clone()).or_default() += 1;
            counts
        });
    let mut file_snapshots = BTreeMap::new();
    for file in files {
        file_snapshots.insert(
            file.clone(),
            CodewikiFileSnapshot {
                content_hash: hash_snapshot_file(project_root, &file)?,
                symbol_count: symbols_by_file.get(&file).copied().unwrap_or_default(),
            },
        );
    }

    let symbols = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| {
            (
                symbol.id.clone(),
                CodewikiSymbolSnapshot {
                    file_path: symbol.file_path.clone(),
                    name: symbol.name.clone(),
                    qualified_name: symbol.qualified_name.clone(),
                    kind: symbol.kind.clone(),
                    line_start: symbol.line_start,
                },
            )
        })
        .collect::<BTreeMap<_, _>>();

    let mut degraded_sources = Vec::new();
    let graph_neighborhoods = match input.graph_availability {
        CodewikiGraphAvailability::Available => Some(graph_neighborhood_fingerprints(
            &symbols,
            &input.graph_edges,
        )),
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.push("graph-truncated".to_string());
            Some(graph_neighborhood_fingerprints(
                &symbols,
                &input.graph_edges,
            ))
        }
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.push("graph-unavailable".to_string());
            None
        }
    };

    Ok(CodewikiIndexSnapshot {
        files: file_snapshots,
        symbols,
        graph_neighborhoods,
        degraded_sources,
    })
}

pub(crate) fn build_codewiki_changes_doc(
    previous: Option<&CodewikiIndexSnapshot>,
    current: &CodewikiIndexSnapshot,
) -> String {
    let baseline = previous.is_none();
    let degraded = !current.degraded_sources.is_empty();
    let mut doc = changes_frontmatter(baseline, degraded, &current.degraded_sources);
    doc.push_str("# Index Changes\n\n");
    doc.push_str("## Current Snapshot\n\n");
    doc.push_str(&format!("- Files: {}\n", current.files.len()));
    doc.push_str(&format!("- Symbols: {}\n", current.symbols.len()));
    match &current.graph_neighborhoods {
        Some(neighborhoods) => {
            doc.push_str(&format!("- Graph neighborhoods: {}\n", neighborhoods.len()));
        }
        None => doc.push_str("- Graph neighborhoods: unavailable\n"),
    }
    doc.push('\n');

    let Some(previous) = previous else {
        doc.push_str("No previous index snapshot was available.\n");
        doc.push_str("This page is the baseline for future index changes.\n");
        return doc;
    };

    let added_files = current
        .files
        .keys()
        .filter(|file| !previous.files.contains_key(*file))
        .cloned()
        .collect::<Vec<_>>();
    let removed_files = previous
        .files
        .keys()
        .filter(|file| !current.files.contains_key(*file))
        .cloned()
        .collect::<Vec<_>>();
    let changed_files = current
        .files
        .iter()
        .filter(|(file, current_file)| {
            previous.files.get(*file).is_some_and(|previous_file| {
                previous_file.content_hash != current_file.content_hash
            })
        })
        .map(|(file, _)| file.clone())
        .collect::<Vec<_>>();
    let new_symbols = current
        .symbols
        .iter()
        .filter(|(id, _)| !previous.symbols.contains_key(*id))
        .map(|(_, symbol)| symbol_label(symbol))
        .collect::<Vec<_>>();
    let removed_symbols = previous
        .symbols
        .iter()
        .filter(|(id, _)| !current.symbols.contains_key(*id))
        .map(|(_, symbol)| symbol_label(symbol))
        .collect::<Vec<_>>();

    write_bullet_section(&mut doc, "Added Files", added_files, "`", "`");
    write_bullet_section(&mut doc, "Removed Files", removed_files, "`", "`");
    write_bullet_section(&mut doc, "Changed Files", changed_files, "`", "`");
    write_bullet_section(&mut doc, "New Symbols", new_symbols, "", "");
    write_bullet_section(&mut doc, "Removed Symbols", removed_symbols, "", "");

    if let (Some(previous_graph), Some(current_graph)) =
        (&previous.graph_neighborhoods, &current.graph_neighborhoods)
    {
        let graph_ids = previous_graph
            .keys()
            .chain(current_graph.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        let changed_graph = graph_ids
            .into_iter()
            .filter(|id| previous_graph.get(id) != current_graph.get(id))
            .map(|id| {
                current
                    .symbols
                    .get(&id)
                    .or_else(|| previous.symbols.get(&id))
                    .map(symbol_label)
                    .unwrap_or_else(|| format!("`{id}`"))
            })
            .collect::<Vec<_>>();
        write_bullet_section(
            &mut doc,
            "Changed Graph Neighborhoods",
            changed_graph,
            "",
            "",
        );
    }

    doc
}

pub(crate) fn build_file_doc(
    file: &str,
    module: String,
    symbols: Vec<Symbol>,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> FileDoc {
    let symbol_docs = symbols
        .into_iter()
        .map(|symbol| {
            let fallback = structural_symbol_purpose(&symbol);
            let generated = maybe_generate(
                generate,
                &prompts::symbol_prompt(&symbol),
                prompts::SYMBOL_SYSTEM,
            )
            .unwrap_or(fallback);
            let component_id = symbol.id.clone();
            let component_label = component_label(&symbol);
            let source_span = SourceSpan::from_symbol(&symbol);
            let purpose = ground_text(
                &generated,
                std::slice::from_ref(&source_span),
                &source_span.citation(),
            );
            SymbolDoc {
                symbol,
                purpose,
                component_id,
                component_label,
                source_span,
            }
        })
        .collect::<Vec<_>>();
    let source_spans = symbol_docs
        .iter()
        .map(|symbol| symbol.source_span.clone())
        .collect::<Vec<_>>();
    let prompt_symbols = symbol_docs
        .iter()
        .map(|symbol| prompts::SymbolSummary {
            name: symbol.symbol.qualified_name.clone(),
            kind: symbol.symbol.kind.clone(),
            component_id: symbol.component_id.clone(),
            component_label: symbol.component_label.clone(),
            line_start: symbol.symbol.line_start,
            line_end: symbol.symbol.line_end,
            purpose: symbol.purpose.clone(),
        })
        .collect::<Vec<_>>();
    let component_ids = symbol_docs
        .iter()
        .map(|symbol| symbol.component_id.clone())
        .collect::<Vec<_>>();
    let fallback = structural_file_summary(file, &symbol_docs);
    let generated = maybe_generate(
        generate,
        &prompts::file_prompt(file, &prompt_symbols),
        prompts::FILE_SYSTEM,
    )
    .unwrap_or(fallback);
    let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

    FileDoc {
        path: file.to_string(),
        module,
        summary,
        source_spans,
        symbols: symbol_docs,
        component_ids,
    }
}

fn hash_snapshot_file(project_root: &Path, file: &str) -> anyhow::Result<String> {
    let canonical_root = project_root
        .canonicalize()
        .map_err(|err| anyhow::anyhow!("failed to resolve codewiki project root: {err}"))?;
    let source_path = project_root.join(file);
    let canonical_source = source_path
        .canonicalize()
        .map_err(|err| anyhow::anyhow!("failed to resolve codewiki source file {file}: {err}"))?;
    if !canonical_source.starts_with(&canonical_root) {
        anyhow::bail!("codewiki source file {file} resolves outside project root");
    }
    hasher::file_content_hash(&canonical_source)
        .map_err(|err| anyhow::anyhow!("failed to hash codewiki source file {file}: {err}"))
}

fn graph_neighborhood_fingerprints(
    symbols: &BTreeMap<String, CodewikiSymbolSnapshot>,
    graph_edges: &[CodewikiGraphEdge],
) -> BTreeMap<String, String> {
    let mut neighborhoods = symbols
        .keys()
        .map(|id| (id.clone(), Vec::<String>::new()))
        .collect::<BTreeMap<_, _>>();
    for edge in graph_edges {
        let edge_key = format!(
            "{}:{}->{}",
            match edge.kind {
                CodewikiGraphEdgeKind::Call => "call",
                CodewikiGraphEdgeKind::Import => "import",
            },
            edge.source_component_id,
            edge.target_component_id
        );
        if let Some(source) = neighborhoods.get_mut(&edge.source_component_id) {
            source.push(edge_key.clone());
        }
        if let Some(target) = neighborhoods.get_mut(&edge.target_component_id) {
            target.push(edge_key);
        }
    }
    neighborhoods
        .into_iter()
        .map(|(id, mut edges)| {
            edges.sort();
            let joined = edges.join("\n");
            (id, hasher::content_hash(joined.as_bytes()))
        })
        .collect()
}

#[derive(Serialize)]
struct ChangesFrontmatter<'a> {
    title: &'a str,
    kind: &'a str,
    generated_by: &'a str,
    trust: &'a str,
    freshness: &'a str,
    baseline: bool,
    degraded: bool,
    degraded_sources: Vec<&'a str>,
}

fn changes_frontmatter(baseline: bool, degraded: bool, degraded_sources: &[String]) -> String {
    let data = ChangesFrontmatter {
        title: "Index Changes",
        kind: "code_changes",
        generated_by: "gcode-codewiki",
        trust: "generated",
        freshness: "indexed",
        baseline,
        degraded,
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
    };
    let yaml = serde_yaml::to_string(&data)
        .expect("codewiki changes frontmatter only contains YAML-serializable data");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);
    let mut out = String::from("---\n");
    out.push_str(yaml);
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str("---\n\n");
    out
}

fn write_bullet_section(
    doc: &mut String,
    title: &str,
    items: Vec<String>,
    prefix: &str,
    suffix: &str,
) {
    doc.push_str(&format!("## {title}\n\n"));
    if items.is_empty() {
        doc.push_str("- None\n\n");
        return;
    }
    for item in items {
        doc.push_str(&format!("- {prefix}{item}{suffix}\n"));
    }
    doc.push('\n');
}

fn symbol_label(symbol: &CodewikiSymbolSnapshot) -> String {
    format!(
        "`{}` {} in `{}`",
        symbol.qualified_name, symbol.kind, symbol.file_path
    )
}

pub(crate) fn build_module_docs(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> Vec<ModuleDoc> {
    let mut module_names = BTreeSet::new();
    for file in files {
        for module in module_ancestors(&file.module) {
            module_names.insert(module);
        }
    }

    let mut module_summaries: BTreeMap<String, String> = BTreeMap::new();
    let mut module_sources: BTreeMap<String, Vec<SourceSpan>> = BTreeMap::new();
    let mut modules = module_names.into_iter().collect::<Vec<_>>();
    modules.sort_by_key(|module| std::cmp::Reverse(module_depth(module)));

    let mut docs = Vec::new();
    for module in modules {
        let direct_files = files
            .iter()
            .filter(|file| file.module == module)
            .map(|file| FileLink {
                path: file.path.clone(),
                summary: file.summary.clone(),
                source_spans: file.source_spans.clone(),
            })
            .collect::<Vec<_>>();
        let child_modules = direct_child_modules(&module, module_summaries.keys())
            .into_iter()
            .map(|child| ModuleLink {
                summary: module_summaries.get(&child).cloned().unwrap_or_default(),
                source_spans: module_sources.get(&child).cloned().unwrap_or_default(),
                module: child,
            })
            .collect::<Vec<_>>();
        let file_summaries = direct_files
            .iter()
            .map(|file| prompts::ChildSummary {
                name: file.path.clone(),
                summary: file.summary.clone(),
            })
            .collect::<Vec<_>>();
        let child_summaries = child_modules
            .iter()
            .map(|module| prompts::ChildSummary {
                name: module.module.clone(),
                summary: module.summary.clone(),
            })
            .collect::<Vec<_>>();
        let component_ids = files
            .iter()
            .filter(|file| file.module == module || module_is_ancestor(&module, &file.module))
            .flat_map(|file| {
                file.symbols
                    .iter()
                    .map(|symbol| format!("{} ({})", symbol.component_label, symbol.component_id))
            })
            .collect::<Vec<_>>();
        let dependency_diagram = render_module_dependency_mermaid(&module, files, graph_edges);
        let call_diagram = render_module_call_mermaid(&module, files, graph_edges);
        let fallback = structural_module_summary(&module, &direct_files, &child_modules);
        let source_spans = collect_link_spans(&direct_files, &child_modules);
        let generated = maybe_generate(
            generate,
            &prompts::module_prompt(&module, &file_summaries, &child_summaries, &component_ids),
            prompts::MODULE_SYSTEM,
        )
        .unwrap_or(fallback);
        let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

        module_summaries.insert(module.clone(), summary.clone());
        module_sources.insert(module.clone(), source_spans.clone());
        docs.push(ModuleDoc {
            module,
            summary,
            source_spans,
            direct_files,
            child_modules,
            component_ids,
            dependency_diagram,
            call_diagram,
            graph_availability,
        });
    }

    docs.sort_by(|a, b| a.module.cmp(&b.module));
    docs
}

pub(crate) fn build_architecture_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> ArchitectureDoc {
    let subsystem_names = files
        .iter()
        .map(|file| file.module.clone())
        .collect::<BTreeSet<_>>();
    let mut degraded_sources = BTreeSet::new();
    match graph_availability {
        CodewikiGraphAvailability::Available => {}
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.insert("graph-truncated".to_string());
        }
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.insert("graph-unavailable".to_string());
        }
    }

    let mut subsystems = Vec::new();
    for module in modules
        .iter()
        .filter(|module| subsystem_names.contains(&module.module))
    {
        let file_summaries = module
            .direct_files
            .iter()
            .map(|file| prompts::ChildSummary {
                name: file.path.clone(),
                summary: file.summary.clone(),
            })
            .collect::<Vec<_>>();
        let child_summaries = module
            .child_modules
            .iter()
            .map(|module| prompts::ChildSummary {
                name: module.module.clone(),
                summary: module.summary.clone(),
            })
            .collect::<Vec<_>>();
        let fallback =
            structural_module_summary(&module.module, &module.direct_files, &module.child_modules);
        let source_spans = collect_link_spans(&module.direct_files, &module.child_modules);
        let generated = maybe_generate(
            generate,
            &prompts::architecture_prompt(
                &module.module,
                &file_summaries,
                &child_summaries,
                &module.component_ids,
            ),
            prompts::ARCHITECTURE_SYSTEM,
        );
        let responsibility = match generated {
            Some(generated) => generated,
            None => {
                degraded_sources.insert("model-unavailable".to_string());
                fallback
            }
        };
        let responsibility = ground_text(
            &responsibility,
            &source_spans,
            &citation_list(&source_spans),
        );

        subsystems.push(ArchitectureSubsystem {
            module: module.module.clone(),
            responsibility,
            source_spans,
        });
    }

    let dependency_diagram = match graph_availability {
        CodewikiGraphAvailability::Unavailable => None,
        CodewikiGraphAvailability::Available | CodewikiGraphAvailability::Truncated => {
            render_architecture_dependency_mermaid(files, graph_edges)
        }
    };
    let source_spans = subsystems
        .iter()
        .flat_map(|subsystem| subsystem.source_spans.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    ArchitectureDoc {
        source_spans,
        subsystems,
        dependency_diagram,
        degraded_sources: degraded_sources.into_iter().collect(),
    }
}

pub(crate) fn build_onboarding_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> OnboardingDoc {
    let entry_points = onboarding_entry_points(files);
    let mut degraded_sources = BTreeSet::new();
    let mut graph_degraded = false;
    let reading_order = match graph_availability {
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.insert("graph-analytics-unavailable".to_string());
            graph_degraded = true;
            Vec::new()
        }
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.insert("graph-truncated".to_string());
            graph_degraded = true;
            ranked_onboarding_steps(files, modules, graph_edges)
        }
        CodewikiGraphAvailability::Available => {
            ranked_onboarding_steps(files, modules, graph_edges)
        }
    };
    if graph_degraded && reading_order.is_empty() {
        degraded_sources.insert("graph-analytics-unavailable".to_string());
    }

    let source_spans = entry_points
        .iter()
        .map(|entry| entry.source_span.clone())
        .chain(
            reading_order
                .iter()
                .flat_map(|step| step_source_spans(&step.module, modules)),
        )
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    OnboardingDoc {
        source_spans,
        entry_points,
        reading_order,
        degraded_sources: degraded_sources.into_iter().collect(),
    }
}

pub(crate) fn build_hotspots_doc(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> HotspotsDoc {
    let mut degraded_sources = BTreeSet::new();
    match graph_availability {
        CodewikiGraphAvailability::Available => {}
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.insert("graph-truncated".to_string());
        }
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.insert("graph-analytics-unavailable".to_string());
            return HotspotsDoc {
                source_spans: Vec::new(),
                hotspots: Vec::new(),
                god_nodes: Vec::new(),
                bridges: Vec::new(),
                degraded_sources: degraded_sources.into_iter().collect(),
            };
        }
    }

    let nodes = hotspot_nodes(files);
    let graph = AnalyticsGraph {
        nodes: nodes
            .values()
            .map(|node| AnalyticsNode {
                id: node.id.clone(),
                kind: node.kind.clone(),
                weight: node
                    .source_span
                    .as_ref()
                    .map(|span| {
                        span.line_end
                            .saturating_sub(span.line_start)
                            .saturating_add(1)
                    })
                    .unwrap_or(1) as f64,
            })
            .collect(),
        edges: graph_edges
            .iter()
            .filter(|edge| {
                nodes.contains_key(&edge.source_component_id)
                    && nodes.contains_key(&edge.target_component_id)
            })
            .map(|edge| AnalyticsEdge {
                source: edge.source_component_id.clone(),
                target: edge.target_component_id.clone(),
                kind: match edge.kind {
                    CodewikiGraphEdgeKind::Call => "call",
                    CodewikiGraphEdgeKind::Import => "import",
                }
                .to_string(),
            })
            .collect(),
    };
    let analytics = graph_analytics::analyze(&graph);
    let centrality = analytics
        .centrality
        .iter()
        .map(|score| (score.node.id.clone(), (score.degree, score.score)))
        .collect::<BTreeMap<_, _>>();

    let hotspots = analytics
        .hotspots
        .into_iter()
        .filter_map(|hotspot| {
            let node = nodes.get(&hotspot.node.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: Some(hotspot.frequency),
                weight: Some(hotspot.weight),
            })
        })
        .collect::<Vec<_>>();
    let god_nodes = analytics
        .god_nodes
        .into_iter()
        .filter_map(|node_ref| {
            let node = nodes.get(&node_ref.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: None,
                weight: None,
            })
        })
        .collect::<Vec<_>>();
    let bridges = analytics
        .bridges
        .into_iter()
        .filter_map(|node_ref| {
            let node = nodes.get(&node_ref.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: None,
                weight: None,
            })
        })
        .collect::<Vec<_>>();
    let source_spans = hotspots
        .iter()
        .chain(god_nodes.iter())
        .chain(bridges.iter())
        .filter_map(|finding| finding.node.source_span.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    HotspotsDoc {
        source_spans,
        hotspots,
        god_nodes,
        bridges,
        degraded_sources: degraded_sources.into_iter().collect(),
    }
}

fn hotspot_nodes(files: &[FileDoc]) -> BTreeMap<String, HotspotNode> {
    files
        .iter()
        .flat_map(|file| {
            file.symbols.iter().map(|symbol| {
                let label = if symbol.symbol.qualified_name.is_empty() {
                    symbol.symbol.name.clone()
                } else {
                    symbol.symbol.qualified_name.clone()
                };
                (
                    symbol.component_id.clone(),
                    HotspotNode {
                        id: symbol.component_id.clone(),
                        kind: symbol.symbol.kind.clone(),
                        label: label.clone(),
                        wikilink: format!("[[code/files/{}|{}]]", file.path, label),
                        file_wikilink: Some(file_wikilink(&file.path)),
                        source_span: Some(symbol.source_span.clone()),
                    },
                )
            })
        })
        .collect()
}

fn onboarding_entry_points(files: &[FileDoc]) -> Vec<OnboardingEntryPoint> {
    let mut entries = Vec::new();
    let mut seen = BTreeSet::new();

    for file in files {
        if is_rust_entry_file(&file.path) {
            let Some(source_span) = file.source_spans.first().cloned().or_else(|| {
                file.symbols
                    .first()
                    .map(|symbol| symbol.source_span.clone())
            }) else {
                continue;
            };
            let description = if file.path.ends_with("main.rs") {
                "Binary entry file".to_string()
            } else {
                "Library entry file".to_string()
            };
            let key = format!("file:{}", file.path);
            if seen.insert(key) {
                entries.push(OnboardingEntryPoint {
                    link: file_wikilink(&file.path),
                    description,
                    source_span,
                });
            }
        }
    }

    for file in files {
        for symbol in &file.symbols {
            if !is_public_api_symbol(&symbol.symbol) {
                continue;
            }
            let signature = symbol
                .symbol
                .signature
                .as_deref()
                .unwrap_or(&symbol.symbol.qualified_name);
            let key = format!("symbol:{}", symbol.symbol.id);
            if seen.insert(key) {
                entries.push(OnboardingEntryPoint {
                    link: file_wikilink(&file.path),
                    description: format!(
                        "{} public API {}",
                        symbol.symbol.qualified_name,
                        inline_code(signature)
                    ),
                    source_span: symbol.source_span.clone(),
                });
            }
        }
    }

    entries
}

fn ranked_onboarding_steps(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Vec<OnboardingStep> {
    let module_names = modules
        .iter()
        .filter(|module| !module.direct_files.is_empty())
        .map(|module| module.module.clone())
        .collect::<BTreeSet<_>>();
    if module_names.is_empty() {
        return Vec::new();
    }

    let component_modules = files
        .iter()
        .flat_map(|file| {
            file.symbols
                .iter()
                .map(|symbol| (symbol.component_id.clone(), file.module.clone()))
        })
        .collect::<BTreeMap<_, _>>();
    let module_edges = module_dependency_edges(graph_edges, &component_modules, &module_names);
    if module_edges.is_empty() {
        return Vec::new();
    }

    let topology = dependency_topology(&module_names, &module_edges);
    let graph = AnalyticsGraph {
        nodes: module_names
            .iter()
            .map(|module| AnalyticsNode {
                id: module.clone(),
                kind: "module".to_string(),
                weight: topology.get(module).copied().unwrap_or_default() as f64,
            })
            .collect(),
        edges: module_edges
            .iter()
            .map(|(source, target)| AnalyticsEdge {
                source: source.clone(),
                target: target.clone(),
                kind: "imports".to_string(),
            })
            .collect(),
    };
    let analytics = graph_analytics::analyze(&graph);
    let module_summaries = modules
        .iter()
        .map(|module| (module.module.clone(), module.summary.clone()))
        .collect::<BTreeMap<_, _>>();
    let centrality = analytics
        .centrality
        .into_iter()
        .map(|score| (score.node.id, (score.degree, score.score)))
        .collect::<BTreeMap<_, _>>();

    let mut steps = module_names
        .into_iter()
        .filter_map(|module| {
            let (degree, score) = centrality.get(&module).copied().unwrap_or_default();
            (degree > 0).then(|| OnboardingStep {
                summary: module_summaries.get(&module).cloned().unwrap_or_default(),
                module,
                degree,
                score,
            })
        })
        .collect::<Vec<_>>();
    steps.sort_by(|left, right| {
        right
            .degree
            .cmp(&left.degree)
            .then_with(|| {
                right
                    .score
                    .partial_cmp(&left.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| {
                topology
                    .get(&left.module)
                    .copied()
                    .unwrap_or_default()
                    .cmp(&topology.get(&right.module).copied().unwrap_or_default())
            })
            .then_with(|| left.module.cmp(&right.module))
    });
    steps
}

fn module_dependency_edges(
    graph_edges: &[CodewikiGraphEdge],
    component_modules: &BTreeMap<String, String>,
    module_names: &BTreeSet<String>,
) -> BTreeSet<(String, String)> {
    graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Import)
        .filter_map(|edge| {
            let source = component_modules.get(&edge.source_component_id)?;
            let target = component_modules.get(&edge.target_component_id)?;
            (source != target && module_names.contains(source) && module_names.contains(target))
                .then(|| (source.clone(), target.clone()))
        })
        .collect()
}

fn dependency_topology(
    module_names: &BTreeSet<String>,
    module_edges: &BTreeSet<(String, String)>,
) -> BTreeMap<String, usize> {
    let mut incoming = module_names
        .iter()
        .map(|module| (module.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut dependents = module_names
        .iter()
        .map(|module| (module.clone(), BTreeSet::<String>::new()))
        .collect::<BTreeMap<_, _>>();

    for (source, target) in module_edges {
        if let Some(count) = incoming.get_mut(source) {
            *count += 1;
        }
        dependents
            .entry(target.clone())
            .or_default()
            .insert(source.clone());
    }

    let mut ready = incoming
        .iter()
        .filter_map(|(module, count)| (*count == 0).then_some(module.clone()))
        .collect::<VecDeque<_>>();
    let mut rank = BTreeMap::new();
    while let Some(module) = ready.pop_front() {
        if rank.contains_key(&module) {
            continue;
        }
        rank.insert(module.clone(), rank.len());
        for dependent in dependents.get(&module).into_iter().flatten() {
            let Some(count) = incoming.get_mut(dependent) else {
                continue;
            };
            *count = count.saturating_sub(1);
            if *count == 0 {
                ready.push_back(dependent.clone());
            }
        }
    }

    for module in module_names {
        if !rank.contains_key(module) {
            rank.insert(module.clone(), rank.len());
        }
    }
    rank
}

fn step_source_spans(module: &str, modules: &[ModuleDoc]) -> Vec<SourceSpan> {
    modules
        .iter()
        .find(|doc| doc.module == module)
        .map(|doc| doc.source_spans.clone())
        .unwrap_or_default()
}

fn is_rust_entry_file(file: &str) -> bool {
    file == "main.rs" || file == "lib.rs" || file.ends_with("/main.rs") || file.ends_with("/lib.rs")
}

fn is_public_api_symbol(symbol: &Symbol) -> bool {
    symbol
        .signature
        .as_deref()
        .is_some_and(|signature| signature.trim_start().starts_with("pub "))
}
