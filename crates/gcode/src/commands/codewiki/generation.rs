use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt::Write as _;
use std::path::Path;

use crate::index::hasher;
use crate::models::Symbol;

use super::{
    AiDepth, AuditContext, BuiltDoc, CodewikiGraphEdge, CodewikiGraphEdgeKind, CodewikiInput,
    CodewikiProgress, DocPruneScope, FeatureCatalogDoc, FileDoc, FileDocPosition, LeadingChunk,
    ModuleDoc, OwnershipMeta, OwnershipOptions, ReusePlan, SourceSpan, SystemModel, TextGenerator,
    TextVerifier, ToolLoopGenerator, build_architecture_doc, build_curated_navigation_docs,
    build_deprecations_doc, build_file_doc, build_hotspots_doc, build_infrastructure_doc,
    build_module_docs_with_filter, build_onboarding_doc, build_ownership_doc, build_repo_doc,
    cluster, cluster_file_modules, file_doc_path, is_ai_generation_failure_code, is_core_file,
    module_doc_path, module_for_file, relationship_facts_for_file, render_architecture_doc,
    render_deprecations_doc, render_feature_catalog_doc, render_file_doc, render_hotspots_doc,
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
        None,
        &mut generate,
        &mut None,
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
    audit: Option<&AuditContext>,
    mut generate: Option<&mut TextGenerator<'_>>,
    mut tool_loop: Option<&mut ToolLoopGenerator<'_>>,
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
        audit,
        &mut generate,
        &mut tool_loop,
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
        None,
        &mut generate,
        &mut None,
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
        None,
        &mut generate,
        &mut None,
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

/// Reference-appendix links for the deterministic analysis/catalog pages,
/// included only for the pages that will actually be emitted this run (#904).
/// Returns `(label, wikilink-target)` pairs; an absent page is never linked, so
/// the repo overview can't dangle.
fn repo_audit_links(
    has_audit: bool,
    has_feature_catalog: bool,
    has_infrastructure: bool,
) -> Vec<(&'static str, &'static str)> {
    let mut links = Vec::new();
    if has_feature_catalog {
        links.push(("Feature catalog", "code/features"));
    }
    if has_infrastructure {
        links.push(("Infrastructure stack", "code/infrastructure"));
    }
    if has_audit {
        links.push(("Deprecations", "code/deprecations"));
    }
    links
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
    // Deterministic audit context (#889): the deprecation index (stamped into
    // each file doc's symbols for the badge + the `code/deprecations.md` page)
    // and the test-gated symbol index (for the file page's test-count collapse).
    // The CLI runtime passes the real context; test/AI-off entry points pass
    // `None` to omit the deprecations page, exactly like `system_model`.
    audit: Option<&AuditContext>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    // Lane B aggregate generator (#978). When present, the aggregate-tier pages
    // (repo overview, architecture, curated navigation/concept/narrative) are
    // produced by the gcode tool loop and hard-fail on a Lane B failure; leaf
    // pages always use the Lane A `generate` one-shot. `None` (tests / AI off)
    // falls the aggregates back to the Lane A path.
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    doc_scope: &DocPruneScope,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    let emit = &mut |doc: BuiltDoc| emit(doc.with_normalized_markdown());
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
            audit.map(|audit| &audit.deprecations),
            audit.map(|audit| &audit.tests),
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
        emit(
            BuiltDoc {
                path: file_doc_path(&file_doc.path),
                content: file_doc
                    .reused_page
                    .clone()
                    .unwrap_or_else(|| render_file_doc(&file_doc)),
                degraded: file_doc.degraded,
                summary: Some(file_doc.summary.clone()),
                neighbors: BTreeSet::new(),
                invalidation_key: None,
                invalidation_key_requires_sources: false,
            }
            // Record the cross-file neighbor set so a caller/import-target edit
            // invalidates this page on the next run (#885, Leaf H).
            .with_neighbors(relationships.neighbor_files(file)),
        )?;
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
                // A module aggregate invalidates through its member files'
                // source hashes (member-set + members hash), recorded as the
                // page's provenance — no separate key or neighbor set needed.
                neighbors: BTreeSet::new(),
                invalidation_key: None,
                invalidation_key_requires_sources: false,
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
        tool_loop,
        verify,
        reuse,
        progress,
    )? {
        emit(doc)?;
    }
    // Audit/analysis pages are deterministic, input-gated projections (#904).
    // Build the infrastructure page once here (reused at its emission site
    // below) and link every page that will actually be emitted into the repo
    // overview's appendix, so they are reachable instead of orphaned.
    let infrastructure_doc = build_infrastructure_doc(system_model);
    let audit_links = repo_audit_links(
        audit.is_some(),
        feature_catalog.is_some(),
        infrastructure_doc.is_some(),
    );
    let (repo_doc, repo_degraded, repo_key) = build_repo_doc(
        &file_docs,
        &module_docs,
        &input.leading_chunks,
        &audit_links,
        generate,
        tool_loop,
        reuse,
        progress,
    )?;
    emit(
        BuiltDoc {
            path: "code/repo.md".to_string(),
            content: repo_doc,
            degraded: repo_degraded,
            summary: None,
            neighbors: BTreeSet::new(),
            invalidation_key: Some(repo_key),
            invalidation_key_requires_sources: true,
        }
        .with_source_sensitive_key(),
    )?;
    progress.emit("generating architecture docs");
    // Architecture is keyed by the SystemModel plus architecture prompt inputs:
    // a function-body edit leaves it alone, while graph/prose evidence changes
    // rebuild it. Test/AI-off entry points pass no model and fall back to the
    // old full source-set reuse.
    let architecture_key = system_model.map(|model| {
        architecture_invalidation_key(
            model,
            &file_docs,
            &module_docs,
            &input.graph_edges,
            &input.leading_chunks,
        )
    });
    let infrastructure_key = system_model.map(infrastructure_invalidation_key);
    let subsystem_names = cluster::subsystem_roots(&files);
    let architecture_sources = span_files(
        &module_docs
            .iter()
            .filter(|module| subsystem_names.contains(&module.module))
            .flat_map(|module| module.source_spans.iter().cloned())
            .collect::<Vec<_>>(),
    );
    let reused_architecture = match architecture_key.as_deref() {
        Some(key) => reuse
            .as_deref_mut()
            .and_then(|plan| plan.reusable_page_keyed("code/_architecture.md", key)),
        None => reuse
            .as_deref_mut()
            .and_then(|plan| plan.reusable_page("code/_architecture.md", &architecture_sources)),
    };
    let architecture_built = match reused_architecture {
        Some(page) => {
            progress.emit("reusing architecture docs (system model unchanged)");
            match architecture_key.clone() {
                Some(key) => BuiltDoc::derived("code/_architecture.md", page, key),
                None => BuiltDoc::healthy("code/_architecture.md", page),
            }
        }
        None => {
            let architecture_doc = build_architecture_doc(
                &file_docs,
                &module_docs,
                &input.graph_edges,
                &input.leading_chunks,
                system_model,
                generate,
                tool_loop,
                progress,
            )?;
            BuiltDoc {
                path: "code/_architecture.md".to_string(),
                content: render_architecture_doc(&architecture_doc),
                degraded: architecture_doc
                    .degraded_sources
                    .iter()
                    .any(|source| is_ai_generation_failure_code(source)),
                summary: None,
                neighbors: BTreeSet::new(),
                invalidation_key: architecture_key.clone(),
                invalidation_key_requires_sources: false,
            }
        }
    };
    emit(architecture_built)?;
    // Deterministic infra-stack page (#892). Built straight from the workspace
    // system model + curated descriptors — no LLM, never degraded. Omitted when
    // no model was supplied (AI-off / test entry points), exactly like the
    // architecture diagrams.
    progress.emit("generating infrastructure docs");
    if let Some(infrastructure_doc) = infrastructure_doc {
        let content = render_infrastructure_doc(&infrastructure_doc);
        emit(match infrastructure_key.clone() {
            Some(key) => BuiltDoc::derived("code/infrastructure.md", content, key),
            None => BuiltDoc::healthy("code/infrastructure.md", content),
        })?;
    }
    // Deterministic feature catalog page (#888). Built straight from the pinned
    // CLI contract JSONs + dispatch resolver — no LLM, never degraded. Omitted
    // when no catalog was supplied (AI-off / test entry points), exactly like
    // the architecture diagrams and the infrastructure stack page.
    progress.emit("generating feature catalog");
    if let Some(catalog) = feature_catalog {
        let content = render_feature_catalog_doc(catalog);
        // Faithful "contract hash" (Leaf H, #893): the feature catalog render is
        // a pure, deterministic projection of the pinned CLI contract, so a
        // digest of its output changes exactly when the contract surface does —
        // a function-body edit leaves it untouched.
        let key = hasher::content_hash(content.as_bytes());
        emit(BuiltDoc::derived("code/features.md", content, key))?;
    }
    // Deterministic audit page (#889): the deprecation aggregate. Built straight
    // from the source scan — no LLM, NEVER degraded. Omitted when no audit
    // context was supplied (AI-off / test entry points), exactly like the
    // feature catalog.
    if let Some(audit) = audit {
        // Faithful "deprecation-set hash" (Leaf H, #893): the page is a
        // deterministic projection of the deprecation scan, so a digest of its
        // rendered output invalidates exactly on those input changes.
        progress.emit("generating deprecations docs");
        let deprecations =
            render_deprecations_doc(&build_deprecations_doc(input, &audit.deprecations));
        let deprecations_key = hasher::content_hash(deprecations.as_bytes());
        emit(BuiltDoc::derived(
            "code/deprecations.md",
            deprecations,
            deprecations_key,
        ))?;
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

fn architecture_invalidation_key(
    system_model: &SystemModel,
    file_docs: &[FileDoc],
    module_docs: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> String {
    let mut key = String::from("architecture:v2\n");
    let _ = writeln!(key, "system={}", system_model.digest());

    for file in file_docs {
        let _ = writeln!(
            key,
            "file\t{}\t{}\t{}",
            file.path, file.module, file.summary
        );
        for span in &file.source_spans {
            push_span_key(&mut key, "file-span", span);
        }
        for component_id in &file.component_ids {
            let _ = writeln!(key, "file-component\t{}\t{}", file.path, component_id);
        }
        for symbol in &file.symbols {
            let _ = writeln!(
                key,
                "symbol\t{}\t{}\t{}\t{}",
                file.path, symbol.component_label, symbol.component_id, symbol.purpose
            );
        }
    }

    for module in module_docs {
        let _ = writeln!(key, "module\t{}\t{}", module.module, module.summary);
        for span in &module.source_spans {
            push_span_key(&mut key, "module-span", span);
        }
        for file in &module.direct_files {
            let _ = writeln!(
                key,
                "module-file\t{}\t{}\t{}",
                module.module, file.path, file.summary
            );
        }
        for child in &module.child_modules {
            let _ = writeln!(
                key,
                "module-child\t{}\t{}\t{}",
                module.module, child.module, child.summary
            );
        }
    }

    let mut edges = graph_edges.iter().collect::<Vec<_>>();
    edges.sort_by(|left, right| {
        edge_kind_key(&left.kind)
            .cmp(edge_kind_key(&right.kind))
            .then_with(|| left.source_component_id.cmp(&right.source_component_id))
            .then_with(|| left.target_component_id.cmp(&right.target_component_id))
    });
    for edge in edges {
        let _ = writeln!(
            key,
            "edge\t{}\t{}\t{}",
            edge_kind_key(&edge.kind),
            edge.source_component_id,
            edge.target_component_id
        );
    }

    for (path, chunk) in leading_chunks {
        let chunk_hash = hasher::content_hash(chunk.content.as_bytes());
        let _ = writeln!(
            key,
            "leading\t{}\t{}\t{}\t{}",
            path, chunk.line_start, chunk.line_end, chunk_hash
        );
    }

    format!("architecture:{}", hasher::content_hash(key.as_bytes()))
}

fn infrastructure_invalidation_key(system_model: &SystemModel) -> String {
    format!("infrastructure:{}", system_model.digest())
}

fn push_span_key(out: &mut String, prefix: &str, span: &SourceSpan) {
    let _ = writeln!(
        out,
        "{}\t{}\t{}\t{}",
        prefix, span.file, span.line_start, span.line_end
    );
}

fn edge_kind_key(kind: &CodewikiGraphEdgeKind) -> &'static str {
    match kind {
        CodewikiGraphEdgeKind::Call => "call",
        CodewikiGraphEdgeKind::Import => "import",
    }
}
