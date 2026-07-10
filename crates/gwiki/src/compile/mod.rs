use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;

use crate::citations::{
    render_source_citations, source_record_matches_path, source_records_for_paths,
};
use crate::explainer::{
    ExplainerGeneration, ExplainerGenerator, ExplainerReport, build_explainer_prompt,
    generate_explainer,
};
use crate::frontmatter::parse_frontmatter;
use crate::paths::derived_markdown_path;
use crate::session::{CompileState, ResearchSession};
use crate::sources::SourceRecord;
use crate::synthesis::{
    ArticleKind, PageWriteKind, PageWriteOutcome, SynthesisInput, SynthesisPrompt, SynthesisSource,
    SynthesizedPage, WritePolicy, relative_path, resolve_article_path, synthesize_article,
    synthesize_source_pages, write_synthesized_page,
};
use crate::{ScopeIdentity, WikiError};

mod collect;
mod index;
mod render;
pub(crate) mod select;

use collect::*;
pub(crate) use index::lock_file;
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiCompileOptions {
    pub target_kind: ArticleKind,
    pub daemon_synthesis_available: bool,
    /// When set, a Lane B generation *failure* hard-fails the compile with a
    /// distinct [`WikiError::Generation`] instead of writing a structural skeleton
    /// page (#982, matching codewiki #978). Off for the Lane A one-shot path.
    pub hard_fail_on_generation_failure: bool,
    /// Frontmatter `aliases` for the synthesized article (observed case
    /// variants of an entity name).
    pub aliases: Vec<String>,
    /// Frontmatter tags appended after the standard `gwiki`/`compiled` pair
    /// (e.g. the `entity` marker on entity concept pages).
    pub extra_tags: Vec<String>,
    /// When false, the compile state is recorded on the in-memory session only
    /// and `_gwiki/research-session.json` is left untouched. Batch conductors
    /// (`gwiki upkeep`) compile many ephemeral sessions against one vault and
    /// must not clobber the user's interactive research checkpoint.
    pub persist_checkpoint: bool,
    /// Quarantine the written article as an untrusted candidate (#17727).
    /// Set by `gwiki upkeep` for LLM-proposed concept pages; interactive
    /// compiles leave it false.
    pub mark_candidate: bool,
}

impl Default for WikiCompileOptions {
    fn default() -> Self {
        Self {
            target_kind: ArticleKind::Topic,
            daemon_synthesis_available: false,
            hard_fail_on_generation_failure: false,
            aliases: Vec::new(),
            extra_tags: Vec::new(),
            persist_checkpoint: true,
            mark_candidate: false,
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
    if request.topic.trim().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "topic",
            message: "compile handoff requires a topic".to_string(),
        });
    }
    let target_page = normalize_target_page(session.scope.root(), request.target_page.as_deref())?;
    if let Some(target_page) = target_page.as_ref() {
        validate_existing_target_identity(target_page, &request.topic)?;
    }
    let write_intent = request.write_intent;
    let handoff_request = CompileRequest {
        topic: request.topic,
        outline: request.outline,
        target_page: request.target_page,
        write_intent,
    };
    let handoff =
        prepare_handoff_with_persistence(session, handoff_request, options.persist_checkpoint)?;

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

    // Resolve the article page before building the synthesis input: a
    // recompile of the same topic with no explicit target must land on the
    // existing article (no slug-suffixed sibling) and feed its current body
    // into the prompt as update-over-create context (#17635).
    let article_page = match &target_page {
        Some(page) => page.clone(),
        None => resolve_article_path(vault_root, &handoff.bundle.topic, options.target_kind),
    };

    let manifest_records = source_records_for_paths(vault_root, &source_paths)?;
    let synthesis_sources = handoff
        .bundle
        .accepted_sources
        .iter()
        .map(|source| SynthesisSource {
            title: source.title.clone(),
            path: source.path.clone(),
            chunks: source.chunks.clone(),
            existing_page: existing_digest_page(vault_root, &manifest_records, &source.path),
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
        existing_page_body: existing_target_page_body(&article_page)?,
        // A variant identical to the page title is dropped: the title is
        // already a resolution key (gobby_core::vault::lint::page_targets), so
        // a title-equal alias is pure redundancy that every upkeep/compile
        // pass would otherwise rewrite (#17642).
        aliases: options
            .aliases
            .iter()
            .filter(|alias| alias.trim() != handoff.bundle.topic.trim())
            .cloned()
            .collect(),
        extra_tags: options.extra_tags.clone(),
        candidate: options.mark_candidate,
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
    let article = synthesize_article(vault_root, &input, article_page, &explainer)?;
    let mut pages = vec![article.clone()];
    pages.extend(synthesize_source_pages(vault_root, &input, &article.path)?);

    // The article (`pages[0]`) is a curated target page. Overwriting it on a
    // recompile is gated so an existing human-facing page is never silently
    // clobbered (#17635). Two signals authorize the overwrite: an explicit
    // `write_intent` flag, or provenance — an existing article that already
    // carries a `synthesis_mode` frontmatter written by this pipeline is owned
    // by the compile lifecycle, so an automated recompile may refresh it in
    // place to fold in newly accepted or re-fetched sources without a human
    // `--write-intent` flag (#17708). A page lacking that provenance
    // (hand-authored, or authored by another tool) still requires explicit
    // merge intent, preserving the anti-clobber guard. The synthesis already
    // feeds the existing body in as update-over-create context, so an
    // authorized overwrite is a merge, not a blind discard.
    //
    // The source stub pages (`pages[1..]`) are deterministic machine digests
    // keyed by source identity — regenerating them in place is lossless ("Used
    // by" backlinks are preserved by `synthesize_source_pages`), so they always
    // overwrite. Gating them on the article's write intent instead makes a
    // recompile fail loud (or, before identity-slug resolution, mint a
    // slug-suffixed sibling) whenever the derived source page already exists,
    // e.g. a second topic sharing a source already compiled by another (#17707).
    if let Some(target_page) = target_page.as_ref() {
        if let Some(parent) = target_page.parent() {
            ensure_compile_target_parent_inside_vault(vault_root, parent)?;
        }
        validate_existing_target_identity(target_page, &handoff.bundle.topic)?;
    }
    let article_policy = if write_intent || existing_page_is_machine_owned(&article.path)? {
        WritePolicy::AllowOverwriteAfterMerge
    } else {
        WritePolicy::RequireMergeIntent
    };
    let mut page_writes = Vec::with_capacity(pages.len());
    for (index, page) in pages.iter().enumerate() {
        let policy = if index == 0 {
            article_policy
        } else {
            WritePolicy::AllowOverwriteAfterMerge
        };
        page_writes.push(write_synthesized_page(vault_root, page, policy)?);
    }
    let scope_identity = session.scope.identity();
    crate::catalog::regenerate(vault_root, &scope_identity)?;
    write_provenance(
        vault_root,
        &article,
        &handoff.bundle.accepted_sources,
        &handoff.bundle.outline,
    )?;
    mark_sources_compiled(vault_root, &source_paths)?;
    log_page_writes(vault_root, &scope_identity, &pages, &page_writes)?;

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

/// Manifest-backed digest page already on disk for an accepted source: compile
/// links the article there instead of writing a duplicate title-slugged stub.
fn existing_digest_page(
    vault_root: &Path,
    records: &[SourceRecord],
    source_path: &Path,
) -> Option<PathBuf> {
    let record = records
        .iter()
        .find(|record| source_record_matches_path(record, vault_root, source_path))?;
    let relative = derived_markdown_path(record).ok()?;
    let page = vault_root.join(relative);
    page.exists().then_some(page)
}

/// Body of an existing compile target, carried into the explainer prompt so a
/// recompile updates the page instead of regenerating it from scratch.
/// Frontmatter is stripped: the prompt needs the prose, and metadata is
/// re-rendered on write.
fn existing_target_page_body(target_page: &Path) -> Result<Option<String>, WikiError> {
    let text = match fs::read_to_string(target_page) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read existing compile target page",
                path: Some(target_page.to_path_buf()),
                source: error,
            });
        }
    };
    let body = match parse_frontmatter(&text) {
        Ok(parsed) => parsed.body.to_string(),
        Err(_) => text,
    };
    Ok(Some(body))
}

fn validate_existing_target_identity(target_page: &Path, topic: &str) -> Result<(), WikiError> {
    let text = match fs::read_to_string(target_page) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read existing compile target page",
                path: Some(target_page.to_path_buf()),
                source: error,
            });
        }
    };
    let parsed = parse_frontmatter(&text).map_err(|error| WikiError::InvalidInput {
        field: "target_page",
        message: format!(
            "existing compile target {} has malformed frontmatter: {error}",
            target_page.display()
        ),
    })?;
    let Some(title) = parsed.metadata.title.as_deref().map(str::trim) else {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: format!(
                "existing compile target {} requires a non-empty frontmatter title",
                target_page.display()
            ),
        });
    };
    if title.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: format!(
                "existing compile target {} requires a non-empty frontmatter title",
                target_page.display()
            ),
        });
    }
    if title != topic.trim() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: format!(
                "existing compile target {} has title {title:?}, expected {:?}",
                target_page.display(),
                topic.trim()
            ),
        });
    }
    Ok(())
}

/// Whether an existing article page was authored by this compile pipeline.
///
/// A machine-owned page carries a `synthesis_mode` frontmatter key (the
/// synthesis route: `daemon`, `fallback`, ...), written by
/// [`crate::synthesis::render`]. Recompiling such a page is a lifecycle refresh,
/// so it may be overwritten without an explicit `--write-intent` flag (#17708).
/// A missing file, unparseable frontmatter, or a page without that key (a
/// hand-authored article, or one from another tool) is treated as not
/// machine-owned, keeping the #17635 anti-clobber guard in force.
fn existing_page_is_machine_owned(target_page: &Path) -> Result<bool, WikiError> {
    let text = match fs::read_to_string(target_page) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read existing compile target page",
                path: Some(target_page.to_path_buf()),
                source: error,
            });
        }
    };
    let Ok(parsed) = parse_frontmatter(&text) else {
        return Ok(false);
    };
    let machine_owned = parsed
        .metadata
        .unknown
        .get("synthesis_mode")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|mode| !mode.trim().is_empty());
    Ok(machine_owned)
}

/// Append one `page_created`/`page_updated` log line per synthesized page,
/// after every compile side effect has succeeded.
fn log_page_writes(
    vault_root: &Path,
    scope: &ScopeIdentity,
    pages: &[SynthesizedPage],
    page_writes: &[PageWriteOutcome],
) -> Result<(), WikiError> {
    let timestamp = crate::support::time::collect_timestamp()?;
    for (page, write) in pages.iter().zip(page_writes) {
        let action = match write.kind {
            PageWriteKind::Created => crate::log::ACTION_PAGE_CREATED,
            PageWriteKind::Overwritten => crate::log::ACTION_PAGE_UPDATED,
        };
        let relative = relative_path(vault_root, &write.path);
        crate::log::append_logs(
            vault_root,
            None,
            &crate::log::LogEntry {
                timestamp: timestamp.clone(),
                scope: scope.clone(),
                action: action.to_string(),
                summary: page.title.clone(),
                artifacts: vec![PathBuf::from(relative)],
            },
        )?;
    }
    Ok(())
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn prepare_handoff(
    session: &mut ResearchSession,
    request: CompileRequest,
) -> Result<CompileOutcome, WikiError> {
    prepare_handoff_with_persistence(session, request, true)
}

fn prepare_handoff_with_persistence(
    session: &mut ResearchSession,
    mut request: CompileRequest,
    persist_checkpoint: bool,
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
    let rendered = render_bundle(&bundle, session.scope.root());

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
    record_compile_state(session, state.clone(), persist_checkpoint)?;

    Ok(CompileOutcome { bundle, state })
}

/// Record compile state on the session, persisting the research checkpoint
/// only when the caller opted in (see [`WikiCompileOptions::persist_checkpoint`]).
fn record_compile_state(
    session: &mut ResearchSession,
    state: CompileState,
    persist_checkpoint: bool,
) -> Result<(), WikiError> {
    if persist_checkpoint {
        session.record_compile_state(state)
    } else {
        session.compile_state = Some(state);
        Ok(())
    }
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
