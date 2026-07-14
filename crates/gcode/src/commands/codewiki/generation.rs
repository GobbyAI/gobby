use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt::Write as _;
use std::num::NonZeroUsize;
use std::path::Path;
use std::sync::{Mutex, mpsc};

use crate::index::hasher;
use crate::models::Symbol;

use super::{
    AiDepth, AuditContext, BuiltDoc, CodewikiAiOutcome, CodewikiGraphEdge, CodewikiGraphEdgeKind,
    CodewikiInput, CodewikiProgress, DocPruneScope, FeatureCatalogDoc, FileDoc, FileDocPosition,
    LeadingChunk, ModuleDoc, OwnershipMeta, OwnershipOptions, PromptTier, RelationshipFacts,
    ReusePlan, SourceSpan, SyncTextGenerator, SyncTextVerifier, SystemModel, TextGenerator,
    TextVerifier, ToolLoopGenerator, VerifyScope, build_architecture_doc,
    build_curated_navigation_docs, build_deprecations_doc, build_file_doc, build_hotspots_doc,
    build_infrastructure_doc, build_module_docs_with_filter, build_onboarding_doc,
    build_ownership_doc, build_repo_doc, cluster, cluster_file_modules, file_doc_path,
    file_module_link_key, is_ai_generation_failure_code, is_core_file, module_child_links_key,
    module_doc_path, module_for_file, relationship_facts_for_file, render_architecture_doc,
    render_deprecations_doc, render_feature_catalog_doc, render_file_doc, render_hotspots_doc,
    render_infrastructure_doc, render_module_doc, render_onboarding_doc, resolve_file_reuse,
    span_files,
};

/// Options for [`generate_hierarchical_docs`], collapsing the former
/// `generate_hierarchical_docs_with_*` wrapper chain (#17534). Field defaults
/// mirror the AI-off/test path: no deterministic inputs, no generators,
/// symbol-depth AI, full verify scope, silent progress, unscoped pruning.
pub(crate) struct GenerateDocsOptions<'g, 'r> {
    pub ownership: Option<(&'r Path, &'r mut OwnershipMeta)>,
    /// Deterministic workspace system model (#891, #17521). Supplies the
    /// evidence graph the architecture page's LLM-composed diagram is verified
    /// against. The CLI runtime passes the real model built from the project
    /// root; test/AI-off callers leave `None` to omit the diagram section.
    pub system_model: Option<&'r SystemModel>,
    /// Deterministic feature catalog (#888), built from the pinned CLI contract
    /// JSONs + dispatch resolver. The CLI runtime passes the real catalog;
    /// test/AI-off callers leave `None` to omit the catalog page, exactly like
    /// `system_model`.
    pub feature_catalog: Option<&'r FeatureCatalogDoc>,
    /// Deterministic audit context (#889): the deprecation index (stamped into
    /// each file doc's symbols for the badge + the `code/deprecations.md` page)
    /// and the test-gated symbol index (for the file page's test-count
    /// collapse). The CLI runtime passes the real context; test/AI-off callers
    /// leave `None` to omit the deprecations page, exactly like `system_model`.
    pub audit: Option<&'r AuditContext>,
    pub generate: Option<&'r mut TextGenerator<'g>>,
    /// Lane B aggregate generator (#978). When present, the aggregate-tier
    /// pages (repo overview, architecture, curated navigation/concept/
    /// narrative) are produced by the gcode tool loop and hard-fail on a
    /// Lane B failure; leaf pages always use the Lane A `generate` one-shot.
    /// `None` (tests / AI off) falls the aggregates back to the Lane A path.
    pub tool_loop: Option<&'r mut ToolLoopGenerator<'g>>,
    pub verify: Option<&'r mut TextVerifier<'g>>,
    pub ai_depth: AiDepth,
    /// Per-file-leaf verification is skipped unless the scope verifies leaves;
    /// aggregate/curated pages verify regardless (gobby-cli #1001).
    pub verify_scope: VerifyScope,
    pub aggregate_ai_outcome: CodewikiAiOutcome,
    pub reuse: Option<&'r mut ReusePlan>,
    /// `None` runs silently ([`CodewikiProgress::silent`]).
    pub progress: Option<&'r mut CodewikiProgress>,
    /// `None` generates the full unscoped doc set ([`DocPruneScope::unscoped`]).
    pub doc_scope: Option<&'r DocPruneScope>,
    /// Where Lane B / nav-plan failure dumps are written (#17533), resolved by
    /// the CLI runtime via [`super::build::resolve_lane_b_dump_dir`] — the
    /// output's `_meta/lane_b/` by default, never among the generated pages.
    /// `None` (tests, library callers) disables dumping.
    pub lane_b_dump_dir: Option<&'r Path>,
    /// Bounded worker pool for Standard-tier (file) page generation
    /// (`--max-workers`, #17532). `None` — the default, and what `--max-workers 1`
    /// resolves to — keeps the byte-identical fully sequential path. `Some`
    /// fans the per-file doc builds (their symbol and file-body LLM calls) out
    /// to the pool; ReusePlan bookkeeping, page emission, and every module/
    /// aggregate/curated build stay serial and in deterministic order.
    pub file_workers: Option<FileGenerationWorkers<'r>>,
}

/// Worker-pool wiring for [`GenerateDocsOptions::file_workers`]: the pool
/// width plus the thread-safe generation/verification callables the workers
/// share. In-flight LLM calls remain additionally capped by the transport's
/// `ai.max_concurrency` permits ([`gobby_core::ai_context::AiLimiter`]), so a
/// wide pool cannot exceed the configured provider concurrency.
#[derive(Clone, Copy)]
pub(crate) struct FileGenerationWorkers<'r> {
    pub workers: NonZeroUsize,
    pub generate: &'r SyncTextGenerator<'r>,
    /// Consulted only when the run's [`VerifyScope`] verifies leaves.
    pub verify: Option<&'r SyncTextVerifier<'r>>,
}

impl Default for GenerateDocsOptions<'_, '_> {
    fn default() -> Self {
        Self {
            ownership: None,
            system_model: None,
            feature_catalog: None,
            audit: None,
            generate: None,
            tool_loop: None,
            verify: None,
            ai_depth: AiDepth::Symbols,
            verify_scope: VerifyScope::All,
            aggregate_ai_outcome: CodewikiAiOutcome::default(),
            reuse: None,
            progress: None,
            doc_scope: None,
            lane_b_dump_dir: None,
            file_workers: None,
        }
    }
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

/// Single generation entry point: builds and emits the hierarchical codewiki
/// doc set for `input`, configured by [`GenerateDocsOptions`].
pub(crate) fn generate_hierarchical_docs(
    input: &CodewikiInput,
    options: GenerateDocsOptions<'_, '_>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    let GenerateDocsOptions {
        ownership,
        system_model,
        feature_catalog,
        audit,
        mut generate,
        mut tool_loop,
        mut verify,
        ai_depth,
        verify_scope,
        aggregate_ai_outcome,
        mut reuse,
        progress,
        doc_scope,
        lane_b_dump_dir,
        file_workers,
    } = options;
    // The generation body threads these as `&mut Option<&mut T>` so builders
    // can reborrow the generator/verifier/reuse plan per page.
    let generate = &mut generate;
    let tool_loop = &mut tool_loop;
    let verify = &mut verify;
    let reuse = &mut reuse;
    let mut silent_progress;
    let progress = match progress {
        Some(progress) => progress,
        None => {
            silent_progress = CodewikiProgress::silent();
            &mut silent_progress
        }
    };
    let unscoped_doc_scope;
    let doc_scope = match doc_scope {
        Some(doc_scope) => doc_scope,
        None => {
            unscoped_doc_scope = DocPruneScope::unscoped();
            &unscoped_doc_scope
        }
    };
    let emit = &mut |doc: BuiltDoc| emit(doc.with_normalized_markdown());
    // Per-file-leaf verification dominates verify cost on large repos.
    // `VerifyScope::Aggregates` (the default) skips it; the aggregate/curated
    // pages below still verify regardless (gobby-cli #1001).
    let verify_leaves = verify_scope.verifies_leaves();
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
    match file_workers {
        None => {
            for (index, file) in files.iter().enumerate() {
                let file_symbols = symbols_by_file.remove(file).unwrap_or_default();
                // Cross-file relationships are derived before the symbols are
                // moved into the file doc; the id set borrows them only within
                // this block.
                let relationships = {
                    let file_symbol_ids = file_symbols
                        .iter()
                        .map(|symbol| symbol.id.as_str())
                        .collect::<HashSet<&str>>();
                    relationship_facts_for_file(
                        file,
                        &file_symbol_ids,
                        &symbols_by_id,
                        &input.graph_edges,
                    )
                };
                let module = file_modules
                    .get(file)
                    .cloned()
                    .unwrap_or_else(|| module_for_file(file));
                let neighbors = relationships.neighbor_files(file);
                let reused = resolve_file_reuse(reuse, file, &module, &neighbors);
                // Leaf verification is gated by `verify_scope`; aggregates skip it.
                let mut leaf_no_verify: Option<&mut TextVerifier<'_>> = None;
                let leaf_verify = if verify_leaves {
                    &mut *verify
                } else {
                    &mut leaf_no_verify
                };
                let file_doc = build_file_doc(
                    file,
                    module,
                    file_symbols,
                    input.leading_chunks.get(file),
                    &relationships,
                    audit.map(|audit| &audit.deprecations),
                    audit.map(|audit| &audit.tests),
                    reused,
                    generate,
                    leaf_verify,
                    ai_depth,
                    &mut |message| progress.emit(message),
                    FileDocPosition {
                        index: index + 1,
                        total: file_total,
                    },
                );
                emit_file_doc(&file_doc, neighbors, emit)?;
                file_docs.push(file_doc);
            }
        }
        Some(pool) => {
            generate_file_docs_pooled(
                pool,
                &files,
                &mut symbols_by_file,
                &file_modules,
                &symbols_by_id,
                input,
                audit,
                reuse,
                verify_leaves,
                ai_depth,
                progress,
                emit,
                &mut file_docs,
            )?;
        }
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
                // page's provenance. A child cluster RENAME keeps that span
                // set (same files, new name), so the child-link set is keyed
                // separately — the persist gate then writes the regenerated
                // page instead of keeping stale child links (#17731).
                neighbors: BTreeSet::new(),
                invalidation_key: Some(module_child_links_key(&module.child_modules)),
                invalidation_key_requires_sources: true,
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
        &input.graph_edges,
        lane_b_dump_dir,
        generate,
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
    let documented_files = files.iter().map(String::as_str).collect::<BTreeSet<_>>();
    let infrastructure_doc = build_infrastructure_doc(system_model, &documented_files);
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
        // Ownership may only link module pages this run actually emitted;
        // raw `file_modules` cluster names can diverge from that set (#18005).
        let emitted_modules = module_docs
            .iter()
            .map(|module| module.module.clone())
            .collect::<BTreeSet<String>>();
        // The page's provenance hashes cannot see a cluster re-partition: the
        // same source files re-cluster under new module names, so an unkeyed
        // page would be retained stale and its module links would dangle,
        // failing publish closed (#18190). Key it on the emitted module set —
        // the rename guard _architecture.md carries (#17731) — while
        // requires_sources keeps ownership content refreshing when the
        // underlying files change.
        let ownership_key = format!(
            "ownership-links:{}",
            hasher::content_hash(
                emitted_modules
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .join("\n")
                    .as_bytes(),
            )
        );
        emit(BuiltDoc {
            path: "code/_ownership.md".to_string(),
            content: build_ownership_doc(
                project_root,
                &files,
                &file_modules,
                &emitted_modules,
                ownership_meta,
                OwnershipOptions::default(),
            )?,
            degraded: false,
            summary: None,
            neighbors: BTreeSet::new(),
            invalidation_key: Some(ownership_key),
            invalidation_key_requires_sources: true,
        })?;
    }
    Ok(())
}

/// Emit one built file page. Shared by the serial and pooled paths so both
/// write byte-identical pages with the same invalidation inputs.
fn emit_file_doc(
    file_doc: &FileDoc,
    neighbors: BTreeSet<String>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    emit(
        BuiltDoc {
            path: file_doc_path(&file_doc.path),
            content: file_doc
                .reused_page
                .clone()
                .unwrap_or_else(|| render_file_doc(file_doc)),
            degraded: file_doc.degraded,
            summary: Some(file_doc.summary.clone()),
            neighbors: BTreeSet::new(),
            // The module link is a render input source hashes cannot see:
            // clustering is global, so an unchanged file can carry a new
            // module this run (#17731). Keying it makes the persist gate
            // write the re-stamped page instead of keeping stale disk
            // content; `requires_sources` keeps the hash checks alongside.
            invalidation_key: Some(file_module_link_key(&file_doc.module)),
            invalidation_key_requires_sources: true,
        }
        // Record the cross-file neighbor set so a caller/import-target edit
        // invalidates this page on the next run (#885, Leaf H).
        .with_neighbors(neighbors),
    )
}

/// One file's inputs for the bounded worker pool, resolved serially in file
/// order before any worker runs — including its [`ReusePlan`] decision, so the
/// plan's `&mut` bookkeeping never crosses a thread.
struct FileJob {
    index: usize,
    file: String,
    module: String,
    symbols: Vec<Symbol>,
    relationships: RelationshipFacts,
    reused: Option<(String, String)>,
}

/// Worker-thread events funneled to the serial owner of progress + emission.
enum WorkerEvent {
    Progress(String),
    Done(usize, Box<FileDoc>),
}

/// `--max-workers N>1` file-page path (#17532): fan the per-file doc builds
/// (their symbol and file-body LLM calls) out to a bounded pool of scoped
/// threads. Everything order-sensitive stays serial on this thread — reuse
/// decisions run in file order before dispatch, and pages are emitted strictly
/// in file order by buffering out-of-order completions — so the emitted doc
/// set is byte-identical to the serial path given the same generator outputs.
#[expect(clippy::too_many_arguments)]
fn generate_file_docs_pooled(
    pool: FileGenerationWorkers<'_>,
    files: &[String],
    symbols_by_file: &mut BTreeMap<String, Vec<Symbol>>,
    file_modules: &HashMap<String, String>,
    symbols_by_id: &HashMap<&str, &Symbol>,
    input: &CodewikiInput,
    audit: Option<&AuditContext>,
    reuse: &mut Option<&mut ReusePlan>,
    verify_leaves: bool,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
    file_docs: &mut Vec<FileDoc>,
) -> anyhow::Result<()> {
    let file_total = files.len();
    let mut neighbor_sets = Vec::with_capacity(file_total);
    let mut jobs = Vec::with_capacity(file_total);
    for (index, file) in files.iter().enumerate() {
        let file_symbols = symbols_by_file.remove(file).unwrap_or_default();
        // Cross-file relationships are derived before the symbols are moved
        // into the job; the id set borrows them only within this block.
        let relationships = {
            let file_symbol_ids = file_symbols
                .iter()
                .map(|symbol| symbol.id.as_str())
                .collect::<HashSet<&str>>();
            relationship_facts_for_file(file, &file_symbol_ids, symbols_by_id, &input.graph_edges)
        };
        let module = file_modules
            .get(file)
            .cloned()
            .unwrap_or_else(|| module_for_file(file));
        let neighbors = relationships.neighbor_files(file);
        let reused = resolve_file_reuse(reuse, file, &module, &neighbors);
        neighbor_sets.push(neighbors);
        jobs.push(FileJob {
            index,
            file: file.clone(),
            module,
            symbols: file_symbols,
            relationships,
            reused,
        });
    }
    let deprecations = audit.map(|audit| &audit.deprecations);
    let tests = audit.map(|audit| &audit.tests);
    let worker_count = pool.workers.get().min(file_total);
    let job_queue = Mutex::new(jobs.into_iter());
    let (event_tx, event_rx) = mpsc::channel();
    std::thread::scope(|scope| -> anyhow::Result<()> {
        for _ in 0..worker_count {
            let event_tx = event_tx.clone();
            let job_queue = &job_queue;
            scope.spawn(move || {
                loop {
                    let job = job_queue
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .next();
                    let Some(FileJob {
                        index,
                        file,
                        module,
                        symbols,
                        relationships,
                        reused,
                    }) = job
                    else {
                        break;
                    };
                    // Adapt the shared thread-safe callables to the `FnMut`
                    // surface the doc builder threads per page.
                    let mut worker_generate = |prompt: &str, system: &str, tier: PromptTier| {
                        (pool.generate)(prompt, system, tier)
                    };
                    let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut worker_generate);
                    // Leaf verification is gated by `verify_scope`; aggregates skip it.
                    let mut worker_verify = pool
                        .verify
                        .filter(|_| verify_leaves)
                        .map(|verify| move |prompt: &str, system: &str| verify(prompt, system));
                    let mut leaf_verify: Option<&mut TextVerifier<'_>> = worker_verify
                        .as_mut()
                        .map(|verify| verify as &mut TextVerifier<'_>);
                    let mut progress_sink = |message: String| {
                        let _ = event_tx.send(WorkerEvent::Progress(message));
                    };
                    let file_doc = build_file_doc(
                        &file,
                        module,
                        symbols,
                        input.leading_chunks.get(&file),
                        &relationships,
                        deprecations,
                        tests,
                        reused,
                        &mut generate,
                        &mut leaf_verify,
                        ai_depth,
                        &mut progress_sink,
                        FileDocPosition {
                            index: index + 1,
                            total: file_total,
                        },
                    );
                    if event_tx
                        .send(WorkerEvent::Done(index, Box::new(file_doc)))
                        .is_err()
                    {
                        // Receiver gone: the run is unwinding; stop cleanly.
                        break;
                    }
                }
            });
        }
        drop(event_tx);
        // Serialize page writes in file order regardless of completion order:
        // buffer out-of-order results and emit the ready prefix.
        let mut completed: BTreeMap<usize, FileDoc> = BTreeMap::new();
        let mut next_emit = 0_usize;
        let mut emit_error = None;
        while let Ok(event) = event_rx.recv() {
            match event {
                WorkerEvent::Progress(message) => progress.emit(message),
                WorkerEvent::Done(index, file_doc) => {
                    completed.insert(index, *file_doc);
                    if emit_error.is_some() {
                        continue;
                    }
                    while let Some(file_doc) = completed.remove(&next_emit) {
                        if let Err(error) = emit_file_doc(
                            &file_doc,
                            std::mem::take(&mut neighbor_sets[next_emit]),
                            emit,
                        ) {
                            emit_error = Some(error);
                            // Stop handing out work; in-flight builds finish,
                            // fail their sends, and the workers exit.
                            job_queue
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner())
                                .by_ref()
                                .for_each(drop);
                            break;
                        }
                        file_docs.push(file_doc);
                        next_emit += 1;
                    }
                }
            }
        }
        match emit_error {
            Some(error) => Err(error),
            None => Ok(()),
        }
    })
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
