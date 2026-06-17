//! Per-page content pass for the curated navigation layer.
//!
//! The structure pass ([`super::concepts`]) names concepts and narrative
//! chapters and gives each a one-line summary. That alone renders thin (a
//! sentence wrapped in provenance). This module runs a second, per-page LLM
//! pass that expands each page into a grounded, multi-section body, with a
//! deterministic structural fallback so `--ai off` and generation failures
//! still produce real structure rather than a bare summary (#853).
//!
//! It is deliberately decoupled from the `ConceptModule`/`NarrativePage`
//! structs: callers pass the primitive member lists, and we hand back a body
//! string. That keeps `concepts.rs` under the 1,000-line rule while this file
//! owns the content-pass + fallback logic.

use std::collections::BTreeMap;
use std::fmt::Write as _;

use super::super::*;

/// Cap on key-symbol evidence rows fed into one content prompt. Bounds prompt
/// size; the structural fallback table reuses the same cap.
const MAX_PAGE_SYMBOL_ROWS: usize = 12;

/// Which curated page voice to generate. Selects the system prompt and the
/// prompt builder: concept pages are reference explainers, narrative pages are
/// guided-tour chapters.
#[derive(Clone, Copy)]
pub(crate) enum CuratedPageKind {
    Concept,
    Narrative,
}

/// Outcome of the per-page content pass.
pub(crate) struct CuratedBody {
    /// The multi-section page body, ready to drop in after the page title.
    /// `None` only when the page has no member content to describe at all.
    pub(crate) body: Option<String>,
    /// True when a content-pass generation was attempted and failed, so the
    /// page fell back to the structural body. Recorded honestly rather than
    /// hidden behind fallback prose (review #1). `false` for `--ai off` skips,
    /// where the structural body is the intended, non-degraded output.
    pub(crate) degraded: bool,
}

/// Run the content pass for one curated page.
///
/// `spans` are the page-specific spans used to ground the generated prose
/// (concept item spans / narrative spans), never the whole-input span set.
#[allow(clippy::too_many_arguments)]
pub(crate) fn curated_page_body(
    kind: CuratedPageKind,
    title: &str,
    summary: &str,
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    spans: &[SourceSpan],
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> CuratedBody {
    let members = member_evidence_rows(member_modules, member_files, module_lookup, file_lookup);
    let symbols = symbol_evidence_rows(member_files, file_lookup);
    if members.is_empty() && symbols.is_empty() {
        return CuratedBody {
            body: None,
            degraded: false,
        };
    }

    let excerpt_take = match kind {
        CuratedPageKind::Concept => prompts::CONCEPT_PAGE_SOURCE_EXCERPTS,
        CuratedPageKind::Narrative => prompts::NARRATIVE_PAGE_SOURCE_EXCERPTS,
    };
    let member_file_docs = member_files
        .iter()
        .filter_map(|file| file_lookup.get(file.as_str()).copied());
    let sources = ranked_source_excerpts(member_file_docs, leading_chunks, excerpt_take);

    let prompt = match kind {
        CuratedPageKind::Concept => {
            prompts::concept_page_prompt(title, summary, &members, &symbols, &sources)
        }
        CuratedPageKind::Narrative => {
            prompts::narrative_page_prompt(title, summary, &members, &symbols, &sources)
        }
    };
    let system = match kind {
        CuratedPageKind::Concept => prompts::CONCEPT_PAGE_SYSTEM,
        CuratedPageKind::Narrative => prompts::NARRATIVE_PAGE_SYSTEM,
    };

    match maybe_generate(generate, &prompt, system, PromptTier::Aggregate) {
        Generation::Generated(text) => {
            let grounded = ground_text(&text, spans, None);
            if grounded.trim().is_empty() {
                CuratedBody {
                    body: Some(structural_body(kind, title, &members, &symbols)),
                    degraded: true,
                }
            } else {
                CuratedBody {
                    body: Some(grounded),
                    degraded: false,
                }
            }
        }
        Generation::Failed => CuratedBody {
            body: Some(structural_body(kind, title, &members, &symbols)),
            degraded: true,
        },
        Generation::Skipped => CuratedBody {
            body: Some(structural_body(kind, title, &members, &symbols)),
            degraded: false,
        },
    }
}

fn member_evidence_rows(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> Vec<prompts::PageEvidenceRow> {
    let mut rows = Vec::new();
    for module in member_modules {
        if let Some(doc) = module_lookup.get(module.as_str()) {
            rows.push(prompts::PageEvidenceRow {
                name: doc.module.clone(),
                kind: "module".to_string(),
                citation: span_citation(&doc.source_spans, &doc.module),
                summary: doc.summary.clone(),
            });
        }
    }
    for file in member_files {
        if let Some(doc) = file_lookup.get(file.as_str()) {
            rows.push(prompts::PageEvidenceRow {
                name: doc.path.clone(),
                kind: "file".to_string(),
                citation: span_citation(&doc.source_spans, &doc.path),
                summary: doc.summary.clone(),
            });
        }
    }
    rows
}

fn symbol_evidence_rows(
    member_files: &[String],
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> Vec<prompts::PageEvidenceRow> {
    let mut rows = Vec::new();
    for file in member_files {
        if let Some(doc) = file_lookup.get(file.as_str()) {
            for symbol in &doc.symbols {
                rows.push(prompts::PageEvidenceRow {
                    name: symbol.symbol.name.clone(),
                    kind: symbol.symbol.kind.clone(),
                    citation: symbol.source_span.citation(),
                    summary: symbol.purpose.clone(),
                });
            }
        }
    }
    // Most important first (most defined symbols → most central), stable on ties.
    rows.sort_by(|a, b| a.name.cmp(&b.name));
    rows.truncate(MAX_PAGE_SYMBOL_ROWS);
    rows
}

fn span_citation(spans: &[SourceSpan], fallback: &str) -> String {
    spans
        .first()
        .map(SourceSpan::citation)
        .unwrap_or_else(|| fallback.to_string())
}

/// Deterministic multi-section fallback body: a real `## Purpose`, a
/// `## Key components` table grounded in symbol citations, and a member list.
/// Mirrors `structural_file_summary` so `--ai off` and content-pass failures
/// still yield structure, not a bare summary.
fn structural_body(
    kind: CuratedPageKind,
    title: &str,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) -> String {
    let mut body = String::new();
    match kind {
        CuratedPageKind::Concept => {
            write_section(
                &mut body,
                "Purpose",
                &format!(
                    "{title} groups the related modules and files listed below; \
                     read the key components for the grounded detail."
                ),
            );
        }
        CuratedPageKind::Narrative => {
            write_section(
                &mut body,
                "Why this matters",
                &format!(
                    "{title} walks through the modules and files listed below; \
                     follow the key components in order, then continue to the linked pages."
                ),
            );
        }
    }

    body.push_str("## Key components\n\n");
    if symbols.is_empty() {
        body.push_str("- No indexed symbols.\n\n");
    } else {
        write_markdown_table_header(&mut body, &["Symbol", "Kind", "Source", "Role"]);
        for row in symbols {
            write_markdown_table_row(
                &mut body,
                [
                    row.name.clone(),
                    row.kind.clone(),
                    row.citation.clone(),
                    row.summary.clone(),
                ],
            );
        }
        body.push('\n');
    }

    if !members.is_empty() {
        body.push_str("## Members\n\n");
        for row in members {
            let _ = writeln!(body, "- `{}` ({}) {}", row.name, row.kind, row.citation);
        }
        body.push('\n');
    }
    body
}
