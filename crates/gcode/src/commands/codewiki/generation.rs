use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use crate::models::Symbol;

use super::{
    AiDepth, BuiltDoc, CodewikiInput, CodewikiProgress, DocPruneScope, FeatureCatalogDoc,
    FileDocPosition, OwnershipMeta, OwnershipOptions, ReusePlan, SystemModel, TextGenerator,
    TextVerifier, build_architecture_doc, build_curated_navigation_docs, build_file_doc,
    build_hotspots_doc, build_infrastructure_doc, build_module_docs_with_filter,
    build_onboarding_doc, build_ownership_doc, build_repo_doc, cluster, cluster_file_modules,
    file_doc_path, is_core_file, module_doc_path, module_for_file, relationship_facts_for_file,
    render_architecture_doc, render_feature_catalog_doc, render_file_doc, render_hotspots_doc,
    render_infrastructure_doc, render_module_doc, render_onboarding_doc, span_files,
};

pub fn generate_hierarchical_docs(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
) -> Vec<(String, String)> {
    generate_hierarchical_docs_with_graph_availability(input, generate)
        .into_iter()
        .map(|doc| (doc.path, doc.content))
        .collect()
}

fn generate_hierarchical_docs_with_graph_availability(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
) -> Vec<BuiltDoc> {
    let mut progress = CodewikiProgress::silent();
    let doc_scope = DocPruneScope::unscoped();
    let mut docs = Vec::new();
    if let Err(error) = generate_hierarchical_docs_core(
        input,
        None,
        None,
        None,
        &mut generate,
        &mut None,
        AiDepth::Symbols,
        &mut None,
        &mut progress,
        &doc_scope,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    ) {
        log::warn!("codewiki generation failed without ownership metadata: {error}");
        return Vec::new();
    }
    docs
}

#[expect(clippy::too_many_arguments)]
pub(crate) fn generate_hierarchical_docs_with_ownership(
    input: &CodewikiInput,
    ownership: Option<(&Path, &mut OwnershipMeta)>,
    system_model: Option<&SystemModel>,
    feature_catalog: Option<&FeatureCatalogDoc>,
    mut generate: Option<&mut TextGenerator<'_>>,
    mut verify: Option<&mut TextVerifier<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    doc_scope: &DocPruneScope,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    generate_hierarchical_docs_core(
        input,
        ownership,
        system_model,
        feature_catalog,
        &mut generate,
        &mut verify,
        ai_depth,
        reuse,
        progress,
        doc_scope,
        emit,
    )
}

#[cfg(test)]
pub(crate) fn generate_hierarchical_docs_with_progress(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    generate_hierarchical_docs_with_reuse(input, generate, ai_depth, &mut None, progress)
}

/// Test entry point that exercises the reuse path without the CLI runtime.
#[cfg(test)]
pub(crate) fn generate_hierarchical_docs_with_reuse(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    let doc_scope = DocPruneScope::unscoped();
    let mut docs = Vec::new();
    if let Err(error) = generate_hierarchical_docs_core(
        input,
        None,
        None,
        None,
        &mut generate,
        &mut None,
        ai_depth,
        reuse,
        progress,
        &doc_scope,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    ) {
        log::warn!("codewiki generation failed without ownership metadata: {error}");
        return Vec::new();
    }
    docs
}

/// Test entry point that threads a verifier alongside the generator, so the
/// grounded verification pass can be exercised end-to-end through the curated
/// page pipeline without the CLI runtime.
#[cfg(test)]
pub(crate) fn generate_hierarchical_docs_with_verify(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
    verify: Option<&mut TextVerifier<'_>>,
    ai_depth: AiDepth,
) -> Vec<BuiltDoc> {
    let mut generate = generate;
    let mut verify = verify;
    let mut progress = CodewikiProgress::silent();
    let doc_scope = DocPruneScope::unscoped();
    let mut docs = Vec::new();
    if let Err(error) = generate_hierarchical_docs_core(
        input,
        None,
        None,
        None,
        &mut generate,
        &mut verify,
        ai_depth,
        &mut None,
        &mut progress,
        &doc_scope,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    ) {
        log::warn!("codewiki generation failed without ownership metadata: {error}");
        return Vec::new();
    }
    docs
}

#[expect(
    clippy::too_many_arguments,
    reason = "core generation threads mutable generator, verifier, reuse, progress, scope, and emit state"
)]
pub(crate) fn generate_hierarchical_docs_core(
    input: &CodewikiInput,
    ownership: Option<(&Path, &mut OwnershipMeta)>,
    // Deterministic workspace system model (#891). Seeds the architecture
    // page's model-derived Mermaid diagrams. The CLI runtime passes the real
    // model built from the project root; test/AI-off entry points pass `None`
    // to omit the diagram section.
    system_model: Option<&SystemModel>,
    // Deterministic feature catalog (#888), built from the pinned CLI contract
    // JSONs + dispatch resolver. The CLI runtime passes the real catalog; the
    // test/AI-off entry points pass `None` to omit the catalog page, exactly
    // like `system_model`.
    feature_catalog: Option<&FeatureCatalogDoc>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    doc_scope: &DocPruneScope,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    let mut files = input
        .files
        .iter()
        .filter(|file| is_core_file(file) && doc_scope.includes_file(file))
        .cloned()
        .collect::<BTreeSet<_>>();
    for symbol in &input.symbols {
        if is_core_file(&symbol.file_path) && doc_scope.includes_file(&symbol.file_path) {
            files.insert(symbol.file_path.clone());
        }
    }
    let files = files.into_iter().collect::<Vec<_>>();

    let mut symbols_by_file: BTreeMap<String, Vec<Symbol>> = BTreeMap::new();
    for symbol in &input.symbols {
        if !is_core_file(&symbol.file_path) || !doc_scope.includes_file(&symbol.file_path) {
            continue;
        }
        symbols_by_file
            .entry(symbol.file_path.clone())
            .or_default()
            .push(symbol.clone());
    }
    for symbols in symbols_by_file.values_mut() {
        symbols.sort_by_key(|symbol| (symbol.line_start, symbol.byte_start, symbol.name.clone()));
    }

    let file_modules = cluster_file_modules(&files, &symbols_by_file, &input.graph_edges);
    // Resolve graph-edge endpoints (symbol component ids) back to their symbols
    // so each file's narrative can name concrete cross-file collaborators (#885).
    let symbols_by_id = input
        .symbols
        .iter()
        .map(|symbol| (symbol.id.as_str(), symbol))
        .collect::<HashMap<&str, &Symbol>>();
    let file_verb = if ai_depth.includes_files() {
        "generating"
    } else {
        "building"
    };
    progress.emit(format!("{file_verb} file docs for {} files", files.len()));
    let file_total = files.len();
    let mut file_docs = Vec::with_capacity(file_total);
    for (index, file) in files.iter().enumerate() {
        let file_symbols = symbols_by_file.remove(file).unwrap_or_default();
        // Cross-file relationships are derived before the symbols are moved into
        // the file doc; the id set borrows them only within this block.
        let relationships = {
            let file_symbol_ids = file_symbols
                .iter()
                .map(|symbol| symbol.id.as_str())
                .collect::<HashSet<&str>>();
            relationship_facts_for_file(file, &file_symbol_ids, &symbols_by_id, &input.graph_edges)
        };
        let file_doc = build_file_doc(
            file,
            file_modules
                .get(file)
                .cloned()
                .unwrap_or_else(|| module_for_file(file)),
            file_symbols,
            input.leading_chunks.get(file),
            &relationships,
            generate,
            verify,
            reuse,
            ai_depth,
            progress,
            FileDocPosition {
                index: index + 1,
                total: file_total,
            },
        );
        emit(BuiltDoc {
            path: file_doc_path(&file_doc.path),
            content: file_doc
                .reused_page
                .clone()
                .unwrap_or_else(|| render_file_doc(&file_doc)),
            degraded: file_doc.degraded,
            summary: Some(file_doc.summary.clone()),
        })?;
        file_docs.push(file_doc);
    }
    progress.emit("generating module docs");
    let module_docs = build_module_docs_with_filter(
        &file_docs,
        &input.leading_chunks,
        &input.graph_edges,
        generate,
        reuse,
        progress,
        &|module| doc_scope.includes_module(module),
        &mut |module| {
            emit(BuiltDoc {
                path: module_doc_path(&module.module),
                content: module
                    .reused_page
                    .clone()
                    .unwrap_or_else(|| render_module_doc(module)),
                degraded: module.degraded,
                summary: Some(module.summary.clone()),
            })
        },
    )?;
    if !doc_scope.is_unscoped() {
        return Ok(());
    }
    for doc in build_curated_navigation_docs(
        &file_docs,
        &module_docs,
        &input.leading_chunks,
        generate,
        verify,
        reuse,
        progress,
    ) {
        emit(doc)?;
    }
    let (repo_doc, repo_degraded) = build_repo_doc(
        &file_docs,
        &module_docs,
        &input.leading_chunks,
        generate,
        reuse,
        progress,
    );
    emit(BuiltDoc {
        path: "code/repo.md".to_string(),
        content: repo_doc,
        degraded: repo_degraded,
        summary: None,
    })?;
    progress.emit("generating architecture docs");
    // Architecture provenance covers every subsystem source, so the page is
    // reusable only when nothing changed at all — then the per-subsystem
    // generation loop is skipped entirely and the on-disk page kept.
    let subsystem_names = cluster::subsystem_roots(&files);
    let architecture_sources = span_files(
        &module_docs
            .iter()
            .filter(|module| subsystem_names.contains(&module.module))
            .flat_map(|module| module.source_spans.iter().cloned())
            .collect::<Vec<_>>(),
    );
    let reused_architecture = reuse
        .as_deref_mut()
        .and_then(|plan| plan.reusable_page("code/_architecture.md", &architecture_sources));
    let architecture_built = match reused_architecture {
        Some(page) => {
            progress.emit("reusing architecture docs (sources unchanged)");
            BuiltDoc::healthy("code/_architecture.md", page)
        }
        None => {
            let architecture_doc = build_architecture_doc(
                &file_docs,
                &module_docs,
                &input.graph_edges,
                &input.leading_chunks,
                system_model,
                generate,
                progress,
            );
            BuiltDoc {
                path: "code/_architecture.md".to_string(),
                content: render_architecture_doc(&architecture_doc),
                degraded: architecture_doc
                    .degraded_sources
                    .iter()
                    .any(|source| source == "model-unavailable"),
                summary: None,
            }
        }
    };
    emit(architecture_built)?;
    // Deterministic infra-stack page (#892). Built straight from the workspace
    // system model + curated descriptors — no LLM, never degraded. Omitted when
    // no model was supplied (AI-off / test entry points), exactly like the
    // architecture diagrams.
    progress.emit("generating infrastructure docs");
    if let Some(infrastructure_doc) = build_infrastructure_doc(system_model) {
        emit(BuiltDoc {
            path: "code/infrastructure.md".to_string(),
            content: render_infrastructure_doc(&infrastructure_doc),
            degraded: false,
            summary: None,
        })?;
    }
    // Deterministic feature catalog page (#888). Built straight from the pinned
    // CLI contract JSONs + dispatch resolver — no LLM, never degraded. Omitted
    // when no catalog was supplied (AI-off / test entry points), exactly like
    // the architecture diagrams and the infrastructure stack page.
    progress.emit("generating feature catalog");
    if let Some(catalog) = feature_catalog {
        emit(BuiltDoc {
            path: "code/features.md".to_string(),
            content: render_feature_catalog_doc(catalog),
            degraded: false,
            summary: None,
        })?;
    }
    progress.emit("generating onboarding docs");
    let onboarding_doc = build_onboarding_doc(
        &file_docs,
        &module_docs,
        &input.graph_edges,
        input.graph_availability,
    );
    emit(BuiltDoc::healthy(
        "code/_onboarding.md",
        render_onboarding_doc(&onboarding_doc),
    ))?;
    progress.emit("generating hotspots docs");
    let hotspots_doc = build_hotspots_doc(&file_docs, &input.graph_edges, input.graph_availability);
    emit(BuiltDoc::healthy(
        "code/_hotspots.md",
        render_hotspots_doc(&hotspots_doc),
    ))?;
    if let Some((project_root, ownership_meta)) = ownership {
        progress.emit("generating ownership docs");
        emit(BuiltDoc::healthy(
            "code/_ownership.md",
            build_ownership_doc(
                project_root,
                &files,
                &file_modules,
                ownership_meta,
                OwnershipOptions::default(),
            )?,
        ))?;
    }
    Ok(())
}
