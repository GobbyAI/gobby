use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use serde::Serialize;

use crate::WikiError;
use crate::citations::render_source_citations;
use crate::explainer::{
    ExplainerGeneration, ExplainerGenerator, ExplainerReport, build_explainer_prompt,
    generate_explainer,
};
use crate::session::{CompileState, ResearchSession};
use crate::synthesis::{
    ArticleKind, PageWriteOutcome, SynthesisInput, SynthesisPrompt, SynthesisSource, WritePolicy,
    synthesize_article, synthesize_source_pages, write_synthesized_page,
};

mod collect;
mod index;
mod render;

use collect::*;
use index::*;
use render::*;

const INDEX_LOCK_TIMEOUT_ENV: &str = "GWIKI_INDEX_LOCK_TIMEOUT_MS";
const DEFAULT_INDEX_LOCK_TIMEOUT_MS: u64 = 5_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileRequest {
    pub topic: String,
    pub outline: Vec<String>,
    pub target_page: Option<PathBuf>,
    pub write_intent: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileOutcome {
    pub bundle: CompileBundle,
    pub state: CompileState,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WikiCompileOptions {
    pub target_kind: ArticleKind,
    pub daemon_synthesis_available: bool,
    /// When set, a Lane B generation *failure* hard-fails the compile with a
    /// distinct [`WikiError::Generation`] instead of writing a structural skeleton
    /// page (#982, matching codewiki #978). Off for the Lane A one-shot path.
    pub hard_fail_on_generation_failure: bool,
}

impl Default for WikiCompileOptions {
    fn default() -> Self {
        Self {
            target_kind: ArticleKind::Topic,
            daemon_synthesis_available: false,
            hard_fail_on_generation_failure: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiCompileOutcome {
    pub handoff_id: String,
    pub article_path: PathBuf,
    pub source_paths: Vec<PathBuf>,
    pub index_path: PathBuf,
    pub page_writes: Vec<PageWriteOutcome>,
    pub prompt: SynthesisPrompt,
    pub explainer: Option<ExplainerReport>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileBundle {
    pub handoff_id: String,
    pub topic: String,
    pub outline: Vec<String>,
    pub accepted_sources: Vec<AcceptedCompileSource>,
    pub citations: Vec<String>,
    pub conflicting_claims: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub target_page: Option<PathBuf>,
    pub write_intent: bool,
    pub path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedCompileSource {
    pub title: String,
    pub path: PathBuf,
    pub chunks: Vec<String>,
    pub chunk_offsets: Vec<AcceptedCompileChunkOffset>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedCompileChunkOffset {
    pub byte_start: usize,
    pub byte_end: usize,
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn compile_to_wiki(
    session: &mut ResearchSession,
    request: CompileRequest,
) -> Result<WikiCompileOutcome, WikiError> {
    compile_to_wiki_with_options(session, request, WikiCompileOptions::default(), None)
}

pub fn compile_to_wiki_with_options(
    session: &mut ResearchSession,
    request: CompileRequest,
    options: WikiCompileOptions,
    generator: Option<ExplainerGenerator<'_>>,
) -> Result<WikiCompileOutcome, WikiError> {
    let target_page = normalize_target_page(session.scope.root(), request.target_page.as_deref())?;
    let write_intent = request.write_intent;
    let handoff_request = CompileRequest {
        topic: request.topic,
        outline: request.outline,
        target_page: None,
        write_intent: false,
    };
    let mut handoff = prepare_handoff(session, handoff_request)?;
    handoff.bundle.target_page = target_page.clone();
    handoff.bundle.write_intent = write_intent;
    handoff.state.write_intent = write_intent;
    session.record_compile_state(handoff.state.clone())?;

    let vault_root = session.scope.root();
    let source_paths: Vec<PathBuf> = handoff
        .bundle
        .accepted_sources
        .iter()
        .map(|source| source.path.clone())
        .collect();
    let mut citations = handoff.bundle.citations.clone();
    extend_unique(
        &mut citations,
        render_source_citations(vault_root, &source_paths)?,
    );

    let synthesis_sources = handoff
        .bundle
        .accepted_sources
        .iter()
        .map(|source| SynthesisSource {
            title: source.title.clone(),
            path: source.path.clone(),
            chunks: source.chunks.clone(),
        })
        .collect();
    let input = SynthesisInput {
        handoff_id: handoff.bundle.handoff_id.clone(),
        topic: handoff.bundle.topic.clone(),
        outline: handoff.bundle.outline.clone(),
        target_kind: options.target_kind,
        accepted_sources: synthesis_sources,
        citations,
        conflicting_claims: handoff.bundle.conflicting_claims.clone(),
        missing_evidence: handoff.bundle.missing_evidence.clone(),
    };
    let explainer_prompt = build_explainer_prompt(vault_root, &input);
    let prompt = SynthesisPrompt {
        system: explainer_prompt.system.to_string(),
        user: explainer_prompt.user.clone(),
        daemon_synthesis_available: options.daemon_synthesis_available,
        tokens_estimated: explainer_prompt.tokens_estimated,
        truncated_sources: explainer_prompt.truncated_sources,
    };
    let explainer = generate_explainer(&input, &explainer_prompt, generator);
    if options.hard_fail_on_generation_failure
        && let ExplainerGeneration::Failed { error } = &explainer
    {
        // Lane B generation failed: hard-fail with a distinct reason instead of
        // writing a structural skeleton page (#982, matching codewiki #978).
        return Err(WikiError::Generation {
            detail: format!(
                "Lane B compile generation failed ({error}); page not written \
                 (no skeleton, no Lane A fallback)"
            ),
        });
    }
    if handoff.bundle.write_intent
        && let Some(target_page) = handoff.bundle.target_page.as_ref()
    {
        let rendered = render_bundle(&handoff.bundle);
        write_target_page(session.scope.root(), target_page, &rendered)?;
    }
    let article = synthesize_article(vault_root, &input, target_page, &explainer)?;
    let mut pages = vec![article.clone()];
    pages.extend(synthesize_source_pages(vault_root, &input, &article.path)?);

    let policy = if write_intent {
        WritePolicy::AllowOverwriteAfterMerge
    } else {
        WritePolicy::RequireMergeIntent
    };
    let mut page_writes = Vec::with_capacity(pages.len());
    for page in &pages {
        page_writes.push(write_synthesized_page(vault_root, page, policy)?);
    }
    update_wiki_index(vault_root, &article)?;
    write_provenance(
        vault_root,
        &article,
        &handoff.bundle.accepted_sources,
        &handoff.bundle.outline,
    )?;
    mark_sources_compiled(vault_root, &source_paths)?;

    Ok(WikiCompileOutcome {
        handoff_id: handoff.bundle.handoff_id,
        article_path: article.path,
        source_paths: pages.iter().skip(1).map(|page| page.path.clone()).collect(),
        index_path: vault_root.join("_index.md"),
        page_writes,
        prompt,
        explainer: article.explainer,
    })
}

pub fn prepare_handoff(
    session: &mut ResearchSession,
    mut request: CompileRequest,
) -> Result<CompileOutcome, WikiError> {
    if request.topic.trim().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "topic",
            message: "compile handoff requires a topic".to_string(),
        });
    }
    request.target_page =
        normalize_target_page(session.scope.root(), request.target_page.as_deref())?;

    let handoff_id = format!(
        "compile-{}-{}",
        slugify(&request.topic),
        unix_timestamp_ms()?
    );
    let bundle_path = session
        .scope
        .root()
        .join(crate::vault::STATE_ROOT)
        .join("compile")
        .join(format!("{handoff_id}.md"));
    let collected_sources = collect_accepted_sources(session)?;
    let bundle = CompileBundle {
        handoff_id: handoff_id.clone(),
        topic: request.topic,
        outline: request.outline,
        accepted_sources: collected_sources.accepted_sources,
        citations: collected_sources.citations,
        conflicting_claims: collected_sources.conflicting_claims,
        missing_evidence: collected_sources.missing_evidence,
        target_page: request.target_page,
        write_intent: request.write_intent,
        path: bundle_path,
    };
    let rendered = render_bundle(&bundle);

    if let Some(parent) = bundle.path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create compile handoff directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(&bundle.path, &rendered).map_err(|error| WikiError::Io {
        action: "write compile handoff bundle",
        path: Some(bundle.path.clone()),
        source: error,
    })?;

    let state = CompileState {
        handoff_id,
        topic: bundle.topic.clone(),
        bundle_path: bundle.path.clone(),
        selected_note_paths: bundle
            .accepted_sources
            .iter()
            .map(|source| source.path.clone())
            .collect(),
        selected_source_titles: bundle
            .accepted_sources
            .iter()
            .map(|source| source.title.clone())
            .collect(),
        citations: bundle.citations.clone(),
        conflicting_claims: bundle.conflicting_claims.clone(),
        missing_evidence: bundle.missing_evidence.clone(),
        write_intent: bundle.write_intent,
    };
    session.record_compile_state(state.clone())?;

    Ok(CompileOutcome { bundle, state })
}

#[derive(Debug, Default)]
pub(crate) struct CollectedSources {
    accepted_sources: Vec<AcceptedCompileSource>,
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
}

pub(crate) fn index_lock_timeout() -> Duration {
    match std::env::var(INDEX_LOCK_TIMEOUT_ENV) {
        Ok(raw) => raw
            .parse::<u64>()
            .ok()
            .filter(|value| *value > 0)
            .map(Duration::from_millis)
            .unwrap_or_else(|| {
                eprintln!("warning: ignoring invalid {INDEX_LOCK_TIMEOUT_ENV}={raw}");
                Duration::from_millis(DEFAULT_INDEX_LOCK_TIMEOUT_MS)
            }),
        Err(_) => Duration::from_millis(DEFAULT_INDEX_LOCK_TIMEOUT_MS),
    }
}

#[cfg(test)]
mod tests;
