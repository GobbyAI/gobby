use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write as _;
use std::path::Path;

use crate::commands::code::runtime::hasher;
use crate::commands::code::{
    AuditContext, BuiltDoc, CodewikiAiOutcome, CodewikiGraphEdge, CodewikiGraphEdgeKind,
    CodewikiInput, CodewikiProgress, DiagramStats, FeatureCatalogDoc, FileDoc, LeadingChunk,
    ModuleDoc, OwnershipMeta, OwnershipOptions, ReusePlan, SourceSpan, SystemModel, TextGenerator,
    TextVerifier, ToolLoopGenerator, build_architecture_doc, build_curated_navigation_docs,
    build_deprecations_doc, build_hotspots_doc, build_infrastructure_doc, build_onboarding_doc,
    build_ownership_doc, build_repo_doc, cluster, content_sensitive_invalidation_key,
    is_ai_generation_failure_code, render_architecture_doc, render_deprecations_doc,
    render_feature_catalog_doc, render_hotspots_doc, render_infrastructure_doc,
    render_onboarding_doc, span_files,
};

#[expect(clippy::too_many_arguments)]
pub(super) fn generate_aggregate_docs(
    input: &CodewikiInput,
    files: &[String],
    file_modules: &HashMap<String, String>,
    file_docs: &[FileDoc],
    module_docs: &[ModuleDoc],
    ownership: Option<(&Path, &str, &mut OwnershipMeta)>,
    system_model: Option<&SystemModel>,
    feature_catalog: Option<&FeatureCatalogDoc>,
    audit: Option<&AuditContext>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    tool_loop_dump_dir: Option<&Path>,
    aggregate_ai_outcome: CodewikiAiOutcome,
    diagram_stats: &mut DiagramStats,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    for doc in build_curated_navigation_docs(
        file_docs,
        module_docs,
        &input.leading_chunks,
        &input.graph_edges,
        tool_loop_dump_dir,
        generate,
        verify,
        reuse,
        &mut *diagram_stats,
        progress,
    )? {
        emit(doc)?;
    }
    // Audit/analysis pages are deterministic, input-gated projections (#904).
    // Build the infrastructure page once here (reused at its emission site
    // below) and link every page that will actually be emitted into the repo
    // overview's appendix, so they are reachable instead of orphaned.
    let documented_files = files.iter().map(String::as_str).collect::<BTreeSet<_>>();
    let infrastructure_doc = build_infrastructure_doc(system_model, &documented_files);
    let audit_links = repo_audit_links(
        audit.is_some(),
        feature_catalog.is_some(),
        infrastructure_doc.is_some(),
    );
    let (repo_doc, repo_degraded, repo_key) = build_repo_doc(
        file_docs,
        module_docs,
        &input.leading_chunks,
        &audit_links,
        generate,
        tool_loop,
        reuse,
        progress,
        aggregate_ai_outcome,
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
            file_docs,
            module_docs,
            &input.graph_edges,
            &input.leading_chunks,
        )
    });
    let infrastructure_key = system_model.map(infrastructure_invalidation_key);
    let subsystem_names = cluster::subsystem_roots(files);
    let architecture_sources = span_files(
        &module_docs
            .iter()
            .filter(|module| subsystem_names.contains(&module.module))
            .flat_map(|module| module.source_spans.iter().cloned())
            .collect::<Vec<_>>(),
    );
    // The model-less fallback still keys on the module names the page links:
    // a synthetic cluster rename keeps every span-file hash, so source-set
    // reuse alone would ship the page with dangling module links (#17731).
    // The full architecture key subsumes this; model-supplied runs ignore it.
    let architecture_fallback_key = format!(
        "architecture-links:{}",
        hasher::content_hash(
            module_docs
                .iter()
                .map(|module| module.module.as_str())
                .chain(file_docs.iter().map(|file| file.module.as_str()))
                .collect::<Vec<_>>()
                .join("\n")
                .as_bytes(),
        )
    );
    let reused_architecture = match architecture_key.as_deref() {
        Some(key) => reuse.as_deref_mut().and_then(|plan| {
            plan.reusable_page_keyed_with_ai_outcome(
                "code/_architecture.md",
                key,
                aggregate_ai_outcome,
            )
        }),
        None => reuse.as_deref_mut().and_then(|plan| {
            plan.reusable_page_keyed_with_sources_and_ai_outcome(
                "code/_architecture.md",
                &architecture_fallback_key,
                &architecture_sources,
                aggregate_ai_outcome,
            )
        }),
    };
    let effective_architecture_key = architecture_key
        .clone()
        .unwrap_or_else(|| architecture_fallback_key.clone());
    let key_requires_sources = architecture_key.is_none();
    let architecture_built = match reused_architecture {
        Some(page) => {
            progress.emit("reusing architecture docs (system model unchanged)");
            let doc = BuiltDoc::derived("code/_architecture.md", page, effective_architecture_key);
            if key_requires_sources {
                doc.with_source_sensitive_key()
            } else {
                doc
            }
        }
        None => {
            let architecture_doc = build_architecture_doc(
                file_docs,
                module_docs,
                &input.graph_edges,
                &input.leading_chunks,
                system_model,
                generate,
                tool_loop,
                &mut *diagram_stats,
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
                invalidation_key: Some(effective_architecture_key),
                invalidation_key_requires_sources: key_requires_sources,
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
        file_docs,
        module_docs,
        &input.graph_edges,
        input.graph_availability,
    );
    emit(BuiltDoc::healthy(
        "code/_onboarding.md",
        render_onboarding_doc(&onboarding_doc),
    ))?;
    progress.emit("generating hotspots docs");
    let hotspots_doc = build_hotspots_doc(file_docs, &input.graph_edges, input.graph_availability);
    emit(BuiltDoc::healthy(
        "code/_hotspots.md",
        render_hotspots_doc(&hotspots_doc),
    ))?;
    if let Some((project_root, project_id, ownership_meta)) = ownership {
        progress.emit("generating ownership docs");
        // Codewiki input comes from the index and can outlive a source file
        // during a long or resumed run. Ownership is emitted near the end, so
        // take a fresh filesystem view here instead of publishing links for
        // indexed paths that have since disappeared (#18483).
        let ownership_files = files
            .iter()
            .filter(|file| project_root.join(file).is_file())
            .cloned()
            .collect::<Vec<_>>();
        // Ownership may only link module pages this run actually emitted;
        // raw `file_modules` cluster names can diverge from that set (#18005).
        let emitted_modules = module_docs
            .iter()
            .map(|module| module.module.clone())
            .collect::<BTreeSet<String>>();
        let ownership_doc = build_ownership_doc(
            project_root,
            project_id,
            &ownership_files,
            file_modules,
            &emitted_modules,
            ownership_meta,
            OwnershipOptions::default(),
        )?;
        // Ownership is deterministic, and its source hashes cannot detect a
        // stale link to a file that disappeared before provenance was hashed.
        // Key the actual rendered content and require the staged body to match
        // it, repairing metadata/body skew before publication (#18190, #18483).
        let ownership_key = content_sensitive_invalidation_key(&ownership_doc);
        emit(BuiltDoc {
            path: "code/_ownership.md".to_string(),
            content: ownership_doc,
            degraded: false,
            summary: None,
            neighbors: BTreeSet::new(),
            invalidation_key: Some(ownership_key),
            invalidation_key_requires_sources: true,
        })?;
    }
    Ok(())
}

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

fn architecture_invalidation_key(
    system_model: &SystemModel,
    file_docs: &[FileDoc],
    module_docs: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> String {
    let mut key = String::from("architecture:v3\n");
    let _ = writeln!(key, "system={}", system_model.digest());

    for file in file_docs {
        let _ = writeln!(
            key,
            "file\t{}\t{}\t{}",
            file.path,
            file.module,
            hasher::content_hash(file.summary.as_bytes())
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
                file.path,
                symbol.component_label,
                symbol.component_id,
                hasher::content_hash(symbol.purpose.as_bytes())
            );
        }
    }

    for module in module_docs {
        let _ = writeln!(
            key,
            "module\t{}\t{}",
            module.module,
            hasher::content_hash(module.summary.as_bytes())
        );
        for span in &module.source_spans {
            push_span_key(&mut key, "module-span", span);
        }
        for file in &module.direct_files {
            let _ = writeln!(
                key,
                "module-file\t{}\t{}\t{}",
                module.module,
                file.path,
                hasher::content_hash(file.summary.as_bytes())
            );
        }
        for child in &module.child_modules {
            let _ = writeln!(
                key,
                "module-child\t{}\t{}\t{}",
                module.module,
                child.module,
                hasher::content_hash(child.summary.as_bytes())
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
