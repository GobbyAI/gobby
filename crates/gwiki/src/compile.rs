use std::fs;
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::WikiError;
use crate::citations::{
    render_source_citations, source_record_matches_path, source_records_for_paths,
};
use crate::provenance::{ProvenanceGraph, ProvenanceLink, SourceChunkRef, WikiSectionRef};
use crate::session::{CompileState, ResearchSession};
use crate::sources::{CompileStatus, SourceManifest};
use crate::synthesis::{
    ArticleKind, PageWriteOutcome, SynthesisInput, SynthesisPrompt, SynthesisSource,
    SynthesizedPage, WritePolicy, build_synthesis_prompt, relative_path, slugify as page_slugify,
    synthesize_article, synthesize_source_pages, wiki_link, write_synthesized_page,
};

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
}

impl Default for WikiCompileOptions {
    fn default() -> Self {
        Self {
            target_kind: ArticleKind::Topic,
            daemon_synthesis_available: false,
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

pub fn compile_to_wiki(
    session: &mut ResearchSession,
    request: CompileRequest,
) -> Result<WikiCompileOutcome, WikiError> {
    compile_to_wiki_with_options(session, request, WikiCompileOptions::default())
}

pub fn compile_to_wiki_with_options(
    session: &mut ResearchSession,
    request: CompileRequest,
    options: WikiCompileOptions,
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
    if handoff.bundle.write_intent
        && let Some(target_page) = handoff.bundle.target_page.as_ref()
    {
        let rendered = render_bundle(&handoff.bundle);
        write_target_page(session.scope.root(), target_page, &rendered)?;
    }

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
        daemon_synthesis_available: options.daemon_synthesis_available,
    };
    let prompt = build_synthesis_prompt(&input);
    let article = synthesize_article(vault_root, &input, target_page)?;
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
    write_provenance(vault_root, &article, &handoff.bundle.accepted_sources)?;
    mark_sources_compiled(vault_root, &source_paths)?;

    Ok(WikiCompileOutcome {
        handoff_id: handoff.bundle.handoff_id,
        article_path: article.path,
        source_paths: pages.iter().skip(1).map(|page| page.path.clone()).collect(),
        index_path: vault_root.join("_index.md"),
        page_writes,
        prompt,
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
        .join(".gwiki")
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

fn update_wiki_index(vault_root: &Path, article: &SynthesizedPage) -> Result<(), WikiError> {
    let lock_path = vault_root.join(".gwiki").join("index.lock");
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create wiki index lock directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open wiki index lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_wiki_index(&lock, &lock_path)?;

    let index_path = vault_root.join("_index.md");
    let mut index = if index_path.exists() {
        fs::read_to_string(&index_path).map_err(|error| WikiError::Io {
            action: "read wiki index",
            path: Some(index_path.clone()),
            source: error,
        })?
    } else {
        "# Wiki Index\n\n".to_string()
    };

    let link = wiki_link(vault_root, &article.path, &article.title);
    if !index.contains("## Compiled pages") {
        if !index.ends_with('\n') {
            index.push('\n');
        }
        index.push_str("\n## Compiled pages\n\n");
    }
    if !index.contains(&link) {
        insert_compiled_page_link(&mut index, &link);
    }

    if let Some(parent) = index_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create wiki index directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(&index_path, index).map_err(|error| WikiError::Io {
        action: "write wiki index",
        path: Some(index_path),
        source: error,
    })?;
    drop(lock);
    Ok(())
}

fn insert_compiled_page_link(index: &mut String, link: &str) {
    let heading = "## Compiled pages";
    let heading_pos = index
        .find(heading)
        .expect("compiled pages heading should exist before insertion");
    let section_body_start = heading_pos + heading.len();
    let mut insertion_point = index[section_body_start..]
        .find("\n## ")
        .map(|offset| section_body_start + offset + 1)
        .unwrap_or(index.len());

    if insertion_point > 0 && !index[..insertion_point].ends_with('\n') {
        index.insert(insertion_point, '\n');
        insertion_point += 1;
    }

    let mut entry = format!("- {link}\n");
    if insertion_point < index.len() && !index[insertion_point..].starts_with('\n') {
        entry.push('\n');
    }
    index.insert_str(insertion_point, &entry);
}

fn write_provenance(
    vault_root: &Path,
    article: &SynthesizedPage,
    sources: &[AcceptedCompileSource],
) -> Result<(), WikiError> {
    let provenance_path = vault_root.join("meta").join("provenance.json");
    let mut graph = if provenance_path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)?
    } else {
        ProvenanceGraph::default()
    };
    let heading =
        first_rendered_article_section(&article.markdown).unwrap_or_else(|| "Overview".to_string());
    let section_id = if heading == "Overview" {
        page_slugify(&article.title)
    } else {
        page_slugify(&heading)
    };
    let section = WikiSectionRef {
        page_path: PathBuf::from(relative_path(vault_root, &article.path)),
        heading,
        section_id,
    };
    let manifest_records = source_records_for_paths(
        vault_root,
        &sources
            .iter()
            .map(|source| source.path.clone())
            .collect::<Vec<_>>(),
    )?;

    for source in sources {
        let source_id = manifest_records
            .iter()
            .find(|record| source_record_matches_path(record, vault_root, &source.path))
            .map(|record| record.id.clone())
            .unwrap_or_else(|| page_slugify(&source.title));
        for (index, chunk) in source.chunks.iter().enumerate() {
            let offset =
                source
                    .chunk_offsets
                    .get(index)
                    .cloned()
                    .unwrap_or(AcceptedCompileChunkOffset {
                        byte_start: 0,
                        byte_end: chunk.len(),
                    });
            graph.add_link(ProvenanceLink {
                source: SourceChunkRef {
                    source_id: source_id.clone(),
                    chunk_id: format!("{source_id}#chunk-{index}"),
                    path: PathBuf::from(relative_path(vault_root, &source.path)),
                    byte_start: offset.byte_start,
                    byte_end: offset.byte_end,
                },
                section: section.clone(),
                claim: Some(chunk.clone()),
            });
        }
    }

    graph.save_to_vault(vault_root)
}

fn first_rendered_article_section(markdown: &str) -> Option<String> {
    markdown.lines().find_map(|line| {
        line.strip_prefix("## ")
            .map(str::trim)
            .filter(|heading| !heading.is_empty())
            .map(ToString::to_string)
    })
}

fn mark_sources_compiled(vault_root: &Path, source_paths: &[PathBuf]) -> Result<(), WikiError> {
    SourceManifest::update(vault_root, |manifest| {
        let mut changed = false;
        for entry in &mut manifest.entries {
            if source_paths
                .iter()
                .any(|path| source_record_matches_path(entry, vault_root, path))
                && entry.compile_status != CompileStatus::Compiled
            {
                entry.compile_status = CompileStatus::Compiled;
                changed = true;
            }
        }
        Ok(changed)
    })
}

#[derive(Debug, Default)]
struct CollectedSources {
    accepted_sources: Vec<AcceptedCompileSource>,
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
}

fn collect_accepted_sources(session: &ResearchSession) -> Result<CollectedSources, WikiError> {
    let mut accepted_sources = Vec::new();
    let mut citations = Vec::new();
    let mut conflicting_claims = Vec::new();
    let mut missing_evidence = Vec::new();

    for note in &session.accepted_notes {
        let path = note_path(session.scope.root(), &note.path);
        require_path_in_scope(&path, session.scope.root())?;
        let text = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read accepted research note",
            path: Some(path.clone()),
            source: error,
        })?;
        let note_sections = parse_note_sections(&text);
        extend_unique(&mut citations, note_sections.citations);
        extend_unique(&mut conflicting_claims, note_sections.conflicting_claims);
        extend_unique(&mut missing_evidence, note_sections.missing_evidence);
        accepted_sources.push(AcceptedCompileSource {
            title: note.title.clone(),
            path,
            chunks: note_sections
                .chunks
                .iter()
                .map(|chunk| chunk.text.clone())
                .collect(),
            chunk_offsets: note_sections
                .chunks
                .iter()
                .map(|chunk| AcceptedCompileChunkOffset {
                    byte_start: chunk.byte_start,
                    byte_end: chunk.byte_end,
                })
                .collect(),
        });
    }

    Ok(CollectedSources {
        accepted_sources,
        citations,
        conflicting_claims,
        missing_evidence,
    })
}

#[derive(Debug, Default)]
struct ParsedNoteSections {
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
    chunks: Vec<ParsedNoteChunk>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedNoteChunk {
    text: String,
    byte_start: usize,
    byte_end: usize,
}

fn parse_note_sections(text: &str) -> ParsedNoteSections {
    let mut sections = ParsedNoteSections::default();
    for (line_start, line) in body_line_spans(text) {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["citation:"]) {
            sections.citations.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["conflict:", "conflicting claim:"]) {
            sections.conflicting_claims.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["gap:", "missing evidence:"]) {
            sections.missing_evidence.push(value.to_string());
            continue;
        }
        let leading = line.len() - line.trim_start().len();
        let trailing = line.len() - line.trim_end().len();
        sections.chunks.push(ParsedNoteChunk {
            text: trimmed.to_string(),
            byte_start: line_start + leading,
            byte_end: line_start + line.len() - trailing,
        });
    }
    sections
}

fn body_line_spans(text: &str) -> Vec<(usize, &str)> {
    let body_start = body_start_offset(text);
    let mut spans = Vec::new();
    let mut offset = body_start;
    for segment in text[body_start..].split_inclusive('\n') {
        let without_newline = segment.strip_suffix('\n').unwrap_or(segment);
        let line = without_newline
            .strip_suffix('\r')
            .unwrap_or(without_newline);
        spans.push((offset, line));
        offset += segment.len();
    }
    spans
}

fn body_start_offset(text: &str) -> usize {
    let Some(first_line_end) = text.find('\n') else {
        return 0;
    };
    if text[..first_line_end].trim() != "---" {
        return 0;
    }

    let mut offset = first_line_end + 1;
    while offset < text.len() {
        let line_end = text[offset..]
            .find('\n')
            .map_or(text.len(), |relative| offset + relative);
        let line = text[offset..line_end].trim_end_matches('\r');
        if line.trim() == "---" {
            return if line_end < text.len() {
                line_end + 1
            } else {
                line_end
            };
        }
        if line_end == text.len() {
            return text.len();
        }
        offset = line_end + 1;
    }
    text.len()
}

fn prefixed_value<'a>(line: &'a str, prefixes: &[&str]) -> Option<&'a str> {
    let lower = line.to_ascii_lowercase();
    for prefix in prefixes {
        if lower.starts_with(prefix) {
            let value = line[prefix.len()..].trim();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

fn extend_unique(target: &mut Vec<String>, values: Vec<String>) {
    let seen = target
        .iter()
        .map(String::as_str)
        .collect::<std::collections::HashSet<_>>();
    let mut additions_seen = std::collections::HashSet::new();
    let mut additions = Vec::new();
    for value in values {
        if seen.contains(value.as_str()) || !additions_seen.insert(value.clone()) {
            continue;
        }
        additions.push(value);
    }
    target.extend(additions);
}

fn note_path(root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}

fn require_path_in_scope(path: &Path, root: &Path) -> Result<(), WikiError> {
    let root = match root.canonicalize() {
        Ok(root) => root,
        Err(error) => {
            return Err(WikiError::Io {
                action: "canonicalize wiki scope root",
                path: Some(root.to_path_buf()),
                source: error,
            });
        }
    };
    let path = match path.canonicalize() {
        Ok(path) => path,
        Err(error) => {
            return Err(WikiError::Io {
                action: "canonicalize accepted research note",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    };
    if path.starts_with(&root) {
        return Ok(());
    }
    Err(WikiError::InvalidInput {
        field: "accepted_note",
        message: format!(
            "accepted research note {} is outside wiki scope {}",
            path.display(),
            root.display()
        ),
    })
}

fn lock_wiki_index(lock: &fs::File, lock_path: &Path) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();

    loop {
        match fs4::FileExt::try_lock(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    // Returning drops the lock file handle, which releases any
                    // fs4 lock acquired by this process through RAII.
                    return Err(WikiError::Io {
                        action: "lock wiki index",
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(Duration::from_millis(25).min(timeout - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock wiki index",
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
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

fn render_bundle(bundle: &CompileBundle) -> String {
    let mut rendered = String::new();
    rendered.push_str("# Compile bundle: ");
    rendered.push_str(&bundle.topic);
    rendered.push_str("\n\n");

    render_list_section(&mut rendered, "Topic outline", &bundle.outline);

    rendered.push_str("## Accepted sources\n\n");
    if bundle.accepted_sources.is_empty() {
        rendered.push_str("- None recorded.\n");
    } else {
        for source in &bundle.accepted_sources {
            rendered.push_str("- ");
            rendered.push_str(&source.title);
            rendered.push_str(" (");
            rendered.push_str(&source.path.to_string_lossy());
            rendered.push_str(")\n");
            for chunk in &source.chunks {
                rendered.push_str("  - ");
                rendered.push_str(chunk);
                rendered.push('\n');
            }
        }
    }
    rendered.push('\n');

    render_list_section(&mut rendered, "Citations", &bundle.citations);
    render_list_section(
        &mut rendered,
        "Conflicting claims",
        &bundle.conflicting_claims,
    );
    render_list_section(&mut rendered, "Missing evidence", &bundle.missing_evidence);

    rendered
}

fn render_list_section(rendered: &mut String, title: &str, values: &[String]) {
    rendered.push_str("## ");
    rendered.push_str(title);
    rendered.push_str("\n\n");
    if values.is_empty() {
        rendered.push_str("- None recorded.\n\n");
        return;
    }
    for value in values {
        rendered.push_str("- ");
        rendered.push_str(value);
        rendered.push('\n');
    }
    rendered.push('\n');
}

fn write_target_page(
    vault_root: &Path,
    target_page: &Path,
    rendered: &str,
) -> Result<(), WikiError> {
    if target_page.exists() {
        return Err(WikiError::InvalidInput {
            field: "write_intent",
            message: format!(
                "existing page {} requires merge/diff handling before overwrite",
                target_page.display()
            ),
        });
    }
    if let Some(parent) = target_page.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create compile target directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
        ensure_compile_target_parent_inside_vault(vault_root, parent)?;
    }
    fs::write(target_page, rendered).map_err(|error| WikiError::Io {
        action: "write compile target page",
        path: Some(target_page.to_path_buf()),
        source: error,
    })
}

fn ensure_compile_target_parent_inside_vault(
    vault_root: &Path,
    parent: &Path,
) -> Result<(), WikiError> {
    let root = vault_root.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve vault root",
        path: Some(vault_root.to_path_buf()),
        source: error,
    })?;
    let parent = parent.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve compile target directory",
        path: Some(parent.to_path_buf()),
        source: error,
    })?;
    if parent.starts_with(root) {
        Ok(())
    } else {
        Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must stay inside the vault".to_string(),
        })
    }
}

fn normalize_target_page(
    vault_root: &Path,
    target_page: Option<&Path>,
) -> Result<Option<PathBuf>, WikiError> {
    let Some(target_page) = target_page else {
        return Ok(None);
    };
    if target_page.is_absolute() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must be vault-relative".to_string(),
        });
    }

    let mut normalized = PathBuf::new();
    for component in target_page.components() {
        match component {
            std::path::Component::Normal(value) => normalized.push(value),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir
            | std::path::Component::RootDir
            | std::path::Component::Prefix(_) => {
                return Err(WikiError::InvalidInput {
                    field: "target_page",
                    message: "compile target page must stay inside the vault".to_string(),
                });
            }
        }
    }
    if normalized.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must identify a wiki document".to_string(),
        });
    }
    Ok(Some(vault_root.join(normalized)))
}

fn slugify(topic: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in topic.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }
    while slug.ends_with('-') {
        slug.pop();
    }
    if slug.is_empty() {
        "handoff".to_string()
    } else {
        slug
    }
}

fn unix_timestamp_ms() -> Result<u64, WikiError> {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| WikiError::Config {
            detail: format!("system clock is before Unix epoch: {error}"),
        })?;
    u64::try_from(duration.as_millis()).map_err(|_| WikiError::Config {
        detail: "system timestamp exceeds u64 milliseconds".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession};

    fn session_with_note(
        scope: &ResearchScope,
        title: &str,
        relative_path: &str,
    ) -> ResearchSession {
        ResearchSession {
            session_id: "research-compile-test".to_string(),
            question: "How should compile handoff work?".to_string(),
            prompt: "Compile source-grounded research".to_string(),
            scope: scope.clone(),
            source_constraints: vec!["accepted notes only".to_string()],
            agent_count: 1,
            dispatch_task_id: Some("#302".to_string()),
            dispatch: None,
            accepted_notes: vec![AcceptedResearchNote {
                title: title.to_string(),
                path: scope.root().join(relative_path),
            }],
            compile_state: None,
        }
    }

    #[test]
    fn compile_bundle_contains_required_sections() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(
            &note_path,
            "---\ntitle: Compile behavior\nsource: daemon notes\n---\n\nCitation: Example Docs, Compile API\nConflict: Workers disagree about overwrite behavior.\nGap: Missing benchmark evidence.\nAccepted chunk about durable synthesis handoff.",
        )
        .expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec![
                    "Durable handoff".to_string(),
                    "Synthesis inputs".to_string(),
                ],
                target_page: Some(PathBuf::from("compile-behavior.md")),
                write_intent: false,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(outcome.bundle.outline.len(), 2);
        assert_eq!(outcome.bundle.accepted_sources.len(), 1);
        assert_eq!(outcome.bundle.citations, vec!["Example Docs, Compile API"]);
        assert_eq!(
            outcome.bundle.conflicting_claims,
            vec!["Workers disagree about overwrite behavior."]
        );
        assert_eq!(
            outcome.bundle.missing_evidence,
            vec!["Missing benchmark evidence."]
        );

        let rendered = std::fs::read_to_string(&outcome.bundle.path).expect("bundle written");
        assert!(rendered.contains("## Topic outline"));
        assert!(rendered.contains("## Accepted sources"));
        assert!(rendered.contains("## Citations"));
        assert!(rendered.contains("## Conflicting claims"));
        assert!(rendered.contains("## Missing evidence"));
    }

    #[test]
    fn compile_handoff_is_non_destructive_by_default() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let page_path = scope.root().join("compile-behavior.md");
        std::fs::write(&page_path, "human-authored wiki page").expect("page written");
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec!["Durable handoff".to_string()],
                target_page: Some(PathBuf::from("compile-behavior.md")),
                write_intent: false,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page retained"),
            "human-authored wiki page"
        );
        assert_ne!(outcome.bundle.path, page_path);
        assert!(!outcome.state.write_intent);
    }

    #[test]
    fn prepare_handoff_does_not_write_target_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let page_path = scope.root().join("compile-behavior.md");
        std::fs::write(&page_path, "human-authored wiki page").expect("page written");
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec!["Durable handoff".to_string()],
                target_page: Some(PathBuf::from("compile-behavior.md")),
                write_intent: true,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page retained"),
            "human-authored wiki page"
        );
        assert!(outcome.state.write_intent);
    }

    #[test]
    fn compile_fails_on_out_of_scope_accepted_note() {
        let in_scope = tempfile::tempdir().expect("in scope tempdir");
        let out_of_scope = tempfile::tempdir().expect("out of scope tempdir");
        let scope = ResearchScope::project(in_scope.path());
        let in_scope_path = scope.root().join("raw/research/in-scope.md");
        std::fs::create_dir_all(in_scope_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&in_scope_path, "Citation: In-scope citation").expect("note written");
        let mut session = session_with_note(&scope, "In scope", "raw/research/in-scope.md");
        session.accepted_notes.push(AcceptedResearchNote {
            title: "Out of scope".to_string(),
            path: out_of_scope.path().join("raw/research/out-of-scope.md"),
        });
        let out_path = out_of_scope.path().join("raw/research/out-of-scope.md");
        std::fs::create_dir_all(out_path.parent().expect("out parent")).expect("out raw dir");
        std::fs::write(&out_path, "Out of scope citation").expect("out note written");

        let err = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Scoped compile".to_string(),
                outline: vec!["Scoped sources".to_string()],
                target_page: None,
                write_intent: false,
            },
        )
        .expect_err("out-of-scope accepted note must fail fast");

        assert!(matches!(
            err,
            WikiError::InvalidInput {
                field: "accepted_note",
                ..
            }
        ));
    }

    #[test]
    fn compile_rejects_absolute_or_escaping_target_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
        let mut absolute_session =
            session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let absolute = prepare_handoff(
            &mut absolute_session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec!["Overview".to_string()],
                target_page: Some(scope.root().join("absolute.md")),
                write_intent: false,
            },
        )
        .expect_err("absolute target page must be rejected");
        assert!(matches!(
            absolute,
            WikiError::InvalidInput {
                field: "target_page",
                ..
            }
        ));

        let mut escaping_session =
            session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
        let escaping = prepare_handoff(
            &mut escaping_session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec!["Overview".to_string()],
                target_page: Some(PathBuf::from("../outside.md")),
                write_intent: false,
            },
        )
        .expect_err("escaping target page must be rejected");
        assert!(matches!(
            escaping,
            WikiError::InvalidInput {
                field: "target_page",
                ..
            }
        ));
    }

    #[cfg(unix)]
    #[test]
    fn compile_rejects_target_page_through_symlinked_parent() {
        let vault = tempfile::tempdir().expect("vault tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        std::os::unix::fs::symlink(outside.path(), vault.path().join("linked"))
            .expect("symlink outside");

        let error = write_target_page(
            vault.path(),
            &vault.path().join("linked/outside.md"),
            "# Outside\n",
        )
        .expect_err("symlinked target parent rejected");

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "target_page",
                ..
            }
        ));
    }

    #[test]
    fn compile_writes_obsidian_markdown() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(
            &note_path,
            concat!(
                "---\n",
                "title: Compile behavior\n",
                "source: daemon notes\n",
                "---\n\n",
                "Citation: Example Docs, Compile API\n",
                "Compile turns accepted notes into source-grounded wiki articles."
            ),
        )
        .expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = compile_to_wiki(
            &mut session,
            CompileRequest {
                topic: "Durable Compile".to_string(),
                outline: vec!["Overview".to_string(), "Evidence".to_string()],
                target_page: None,
                write_intent: false,
            },
        )
        .expect("wiki articles compiled");

        let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
        assert!(
            outcome
                .article_path
                .ends_with("wiki/topics/durable-compile.md")
        );
        assert!(page.starts_with("---\n"));
        assert!(page.contains("title: \"Durable Compile\""));
        assert!(page.contains("source_kind: \"topic\""));
        assert!(page.contains("[[wiki/sources/compile-behavior|Compile behavior]]"));
        assert!(page.contains("Example Docs, Compile API"));

        let source_page = scope.root().join("wiki/sources/compile-behavior.md");
        assert!(source_page.exists());
        let provenance =
            std::fs::read_to_string(scope.root().join("meta/provenance.json")).expect("provenance");
        assert!(provenance.contains("wiki/topics/durable-compile.md"));
        assert!(provenance.contains("raw/research/compile.md"));
        let provenance: ProvenanceGraph =
            serde_json::from_str(&provenance).expect("parse provenance");
        let source = &provenance.links()[0].source;
        assert!(source.byte_start > 0);
        assert!(source.byte_end > source.byte_start);
    }

    #[test]
    fn index_update_preserves_unrelated_entries() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        std::fs::write(
            scope.root().join("_index.md"),
            "# Wiki Index\n\n- [[wiki/topics/existing|Existing Entry]]\n",
        )
        .expect("index written");
        let note_path = scope.root().join("raw/research/index.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Index updates keep unrelated entries.").expect("note written");
        let mut session = session_with_note(&scope, "Index behavior", "raw/research/index.md");

        compile_to_wiki(
            &mut session,
            CompileRequest {
                topic: "Index Preservation".to_string(),
                outline: vec!["Overview".to_string()],
                target_page: None,
                write_intent: false,
            },
        )
        .expect("wiki article compiled");

        let index = std::fs::read_to_string(scope.root().join("_index.md")).expect("index read");
        assert!(index.contains("[[wiki/topics/existing|Existing Entry]]"));
        assert!(index.contains("[[wiki/topics/index-preservation|Index Preservation]]"));
    }
}
