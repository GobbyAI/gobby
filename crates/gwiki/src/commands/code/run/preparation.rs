use gobby_code::codewiki_facts::{FileId, ScopeSelector};

use crate::commands::code::{
    AuditContext, CodeEngineRuntime, CodewikiInput, CodewikiProgress, CommitStamp, DocPruneScope,
    FeatureCatalogDoc, Symbol, SystemModel, build_audit_context, build_feature_catalog_doc,
    build_system_model, fetch_codewiki_graph_edges, in_scope,
};

use super::{
    capture_commit_stamp, codewiki_doc_scope, load_leading_chunks, load_symbols_for_codewiki,
    should_document_file,
};

pub(super) struct PreparedRun {
    pub input: CodewikiInput,
    pub scopes: Vec<String>,
    pub doc_scope: DocPruneScope,
    pub commit_stamp: Option<CommitStamp>,
    pub system_model: SystemModel,
    pub feature_catalog: Option<FeatureCatalogDoc>,
    pub audit_context: AuditContext,
}

pub(super) fn prepare_run(
    ctx: &CodeEngineRuntime,
    scope_args: &[String],
    complete_scope: bool,
    include_docs: bool,
    edge_limit: usize,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<PreparedRun> {
    if complete_scope && scope_args.is_empty() {
        anyhow::bail!("--complete-scope requires at least one --scope path");
    }
    let commit_stamp = capture_commit_stamp(&ctx.project_root);
    let scopes = scope_args
        .iter()
        .map(|value| super::super::runtime::normalize_file_arg(ctx, value))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let selector = if scopes.is_empty() {
        ScopeSelector::all()
    } else {
        ScopeSelector::paths(scopes.iter().cloned())
    };

    progress.emit("loading indexed files");
    let files = ctx
        .facts
        .scoped_files(&selector)?
        .into_iter()
        .filter(|file| should_document_file(&file.path, include_docs))
        .map(|file| file.path)
        .filter(|file| in_scope(file, &scopes))
        .collect::<Vec<_>>();
    let symbols = load_symbols_for_codewiki(&files, progress, |paths| {
        let file_ids = paths.iter().cloned().map(FileId::new).collect::<Vec<_>>();
        Ok(ctx
            .facts
            .symbols_in(&file_ids)?
            .into_iter()
            .map(|symbol| Symbol::from_fact(symbol, &ctx.project_id))
            .collect())
    })?;

    progress.emit("loading leading content chunks");
    let leading_chunks = load_leading_chunks(ctx, &files)?;
    progress.emit(format!(
        "fetching graph edges for {} files and {} symbols (limit {})",
        files.len(),
        symbols.len(),
        edge_limit
    ));
    let graph = fetch_codewiki_graph_edges(ctx, &files, &symbols, edge_limit)?;
    let input = CodewikiInput {
        files,
        graph_edges: graph.edges,
        graph_availability: graph.availability,
        symbols,
        leading_chunks,
    };

    let system_model = build_system_model(&ctx.project_root);
    let feature_catalog = build_feature_catalog_doc(&ctx.project_root, &input.files);
    let audit_context = build_audit_context(&ctx.project_root, &input);
    let doc_scope = codewiki_doc_scope(&scopes, complete_scope);

    Ok(PreparedRun {
        input,
        scopes,
        doc_scope,
        commit_stamp,
        system_model,
        feature_catalog,
        audit_context,
    })
}
