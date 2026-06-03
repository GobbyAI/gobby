use super::*;

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
            let component_id = component_id(&symbol);
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
