mod aggregates;
mod files;

use std::collections::BTreeSet;
use std::num::NonZeroUsize;
use std::path::Path;

use self::{aggregates::generate_aggregate_docs, files::generate_file_docs};

use super::{
    AiDepth, AuditContext, BuiltDoc, CodewikiAiOutcome, CodewikiInput, CodewikiProgress,
    DiagramStats, DocPruneScope, FeatureCatalogDoc, OwnershipMeta, ReusePlan, SyncTextGenerator,
    SyncTextVerifier, SystemModel, TextGenerator, TextVerifier, ToolLoopGenerator, VerifyScope,
    build_module_docs_with_filter, module_child_links_key, module_doc_path, render_module_doc,
};

/// Options for [`generate_hierarchical_docs`], collapsing the former
/// `generate_hierarchical_docs_with_*` wrapper chain (#17534). Field defaults
/// mirror the AI-off/test path: no deterministic inputs, no generators,
/// symbol-depth AI, full verify scope, silent progress, unscoped pruning.
pub(crate) struct GenerateDocsOptions<'g, 'r> {
    /// Ownership inputs, in order: project root, project id, and mutable
    /// metadata used to preserve deterministic ownership assignments.
    pub ownership: Option<(&'r Path, &'r str, &'r mut OwnershipMeta)>,
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
    /// Tool-loop aggregate generator (#978). When present, repo overview and
    /// architecture pages are produced by the gcode tool loop and hard-fail on
    /// a tool-loop failure; leaf and curated pages use one-shot `generate`.
    /// `None` (tests / AI off) falls those aggregates back to one-shot generation.
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
    /// Where tool-loop / nav-plan failure dumps are written (#17533), resolved by
    /// the CLI runtime via [`super::build::resolve_tool_loop_dump_dir`] — the
    /// output's `_meta/tool_loop/` by default, never among the generated pages.
    /// `None` (tests, library callers) disables dumping.
    pub tool_loop_dump_dir: Option<&'r Path>,
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
            tool_loop_dump_dir: None,
            file_workers: None,
        }
    }
}

/// Single generation entry point: builds and emits the hierarchical codewiki
/// doc set for `input`, configured by [`GenerateDocsOptions`].
pub(crate) fn generate_hierarchical_docs(
    input: &CodewikiInput,
    options: GenerateDocsOptions<'_, '_>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<DiagramStats> {
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
        tool_loop_dump_dir,
        file_workers,
    } = options;
    // The generation body threads these as `&mut Option<&mut T>` so builders
    // can reborrow the generator/verifier/reuse plan per page.
    let generate = &mut generate;
    let tool_loop = &mut tool_loop;
    let verify = &mut verify;
    let reuse_enabled = reuse.is_some();
    let reuse = &mut reuse;
    let mut silent_progress;
    let progress = match progress {
        Some(progress) => progress,
        None => {
            silent_progress = CodewikiProgress::silent();
            &mut silent_progress
        }
    };
    let mut diagram_stats = DiagramStats::default();
    let unscoped_doc_scope;
    let doc_scope = match doc_scope {
        Some(doc_scope) => doc_scope,
        None => {
            unscoped_doc_scope = DocPruneScope::unscoped();
            &unscoped_doc_scope
        }
    };
    diagram_stats.partial = reuse_enabled || !doc_scope.is_unscoped();
    let emit = &mut |doc: BuiltDoc| emit(doc.with_normalized_markdown());
    // Per-file-leaf verification dominates verify cost on large repos.
    // `VerifyScope::Aggregates` (the default) skips it; the aggregate/curated
    // pages below still verify regardless (gobby-cli #1001).
    let verify_leaves = verify_scope.verifies_leaves();
    let file_output = generate_file_docs(
        input,
        doc_scope,
        file_workers,
        audit,
        reuse,
        verify_leaves,
        ai_depth,
        progress,
        generate,
        verify,
        emit,
    )?;
    let files = file_output.files;
    let file_modules = file_output.file_modules;
    let file_docs = file_output.file_docs;
    progress.emit("generating module docs");
    let module_docs = build_module_docs_with_filter(
        &file_docs,
        &input.leading_chunks,
        &input.graph_edges,
        input.graph_availability,
        generate,
        reuse,
        &mut diagram_stats,
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
        return Ok(diagram_stats);
    }
    generate_aggregate_docs(
        input,
        &files,
        &file_modules,
        &file_docs,
        &module_docs,
        ownership,
        system_model,
        feature_catalog,
        audit,
        generate,
        tool_loop,
        verify,
        reuse,
        tool_loop_dump_dir,
        aggregate_ai_outcome,
        &mut diagram_stats,
        progress,
        emit,
    )?;
    Ok(diagram_stats)
}
