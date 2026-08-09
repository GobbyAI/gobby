use std::path::Path;

use gobby_core::ai::AiNoticeKind;

use crate::commands::code::{
    CodeEngineRuntime, CodewikiIndexSnapshot, CodewikiPublication, CodewikiRunSummary, CommitStamp,
    DiagramStats, DocPruneScope, DocSink, SystemModel, build_truth_digest, write_truth_digest,
};

use super::ai::AiRunNotices;

pub(super) struct RunCounts {
    pub generated_pages: usize,
    pub files: usize,
    pub modules: usize,
    pub symbols: usize,
}

pub(super) struct FinalizeRun<'a> {
    pub ctx: &'a CodeEngineRuntime,
    pub out_dir: String,
    pub publication: CodewikiPublication,
    pub stage_path: &'a Path,
    pub sink: DocSink<'a>,
    pub index_snapshot: Option<CodewikiIndexSnapshot>,
    pub doc_scope: DocPruneScope,
    pub system_model: &'a SystemModel,
    pub commit_stamp: Option<&'a CommitStamp>,
    pub diagram_stats: DiagramStats,
    pub counts: RunCounts,
    pub ai_enabled: bool,
    pub notices: &'a mut AiRunNotices,
}

pub(super) fn finalize_run(mut run: FinalizeRun<'_>) -> anyhow::Result<CodewikiRunSummary> {
    run.sink.set_diagram_stats(run.diagram_stats);
    let degraded_pages = run.sink.degraded_docs().to_vec();
    if !degraded_pages.is_empty() && !run.ctx.quiet {
        run.notices
            .warn_once(run.ctx, Some(AiNoticeKind::GenerationFailed));
        eprintln!(
            "codewiki: {} page(s) degraded to structural fallback (AI content \
             pass failed): {}",
            degraded_pages.len(),
            degraded_pages.join(", ")
        );
    }
    run.sink.finish(run.index_snapshot)?;
    if run.doc_scope.is_unscoped() {
        let truth_digest = build_truth_digest(
            run.system_model,
            &run.ctx.project_id,
            run.counts.files,
            run.counts.modules,
            run.commit_stamp,
        );
        write_truth_digest(run.stage_path, &run.doc_scope, &truth_digest)?;
    }

    let changed_paths = run.publication.publish()?;
    let skipped = run
        .counts
        .generated_pages
        .saturating_sub(changed_paths.len());
    Ok(CodewikiRunSummary {
        command: "codewiki",
        project_id: run.ctx.project_id.clone(),
        project_root: run.ctx.project_root.display().to_string(),
        out_dir: run.out_dir,
        generated_pages: run.counts.generated_pages,
        changed_paths,
        skipped,
        files: run.counts.files,
        modules: run.counts.modules,
        symbols: run.counts.symbols,
        ai_enabled: run.ai_enabled,
        degraded_pages,
    })
}
