use std::collections::BTreeSet;

use super::super::{
    AiDepth, CodewikiProgress, FileDoc, Generation, LeadingChunk, PromptTier, ReusePlan,
    SourceSpan, SymbolDoc, TextGenerator, TextVerifier, VerifyNote, VerifyOutcome, citation_list,
    component_label, file_doc_path, ground_text, maybe_generate, prompts, structural_file_summary,
    structural_symbol_purpose, verify_with_notes, write_section,
};
use crate::models::Symbol;

/// 1-based position of a file within the generation run, for progress output.
#[derive(Clone, Copy, Debug)]
pub(crate) struct FileDocPosition {
    pub(crate) index: usize,
    pub(crate) total: usize,
}

#[expect(clippy::too_many_arguments)]
pub(crate) fn build_file_doc(
    file: &str,
    module: String,
    symbols: Vec<Symbol>,
    leading_chunk: Option<&LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    position: FileDocPosition,
) -> FileDoc {
    // A file doc's provenance cites only its own file, so reuse is decided by
    // that single hash. Reuse skips every symbol and file LLM call; the
    // recorded summary still feeds module prompts and pages.
    let sources = BTreeSet::from([file.to_string()]);
    let reused = reuse
        .as_deref_mut()
        .and_then(|plan| plan.reusable_page_with_summary(&file_doc_path(file), &sources));
    let file_verb = if reused.is_some() {
        "reusing"
    } else if ai_depth.includes_files() {
        "generating"
    } else {
        "building"
    };
    progress.emit(format!(
        "{file_verb} file doc file {}/{} {}",
        position.index, position.total, file
    ));
    let symbol_total = symbols.len();
    let mut model_degraded = false;
    let mut degraded_sources = BTreeSet::new();
    let mut verify_notes = Vec::new();
    let symbol_docs = symbols
        .into_iter()
        .enumerate()
        .map(|(index, symbol)| {
            let fallback = structural_symbol_purpose(&symbol);
            let generated = if reused.is_none() && ai_depth.includes_symbols() {
                progress.emit(format!(
                    "generating symbol doc file {}/{} symbol {}/{} {}",
                    position.index,
                    position.total,
                    index + 1,
                    symbol_total,
                    symbol.qualified_name
                ));
                maybe_generate(
                    generate,
                    &prompts::symbol_prompt(&symbol),
                    prompts::SYMBOL_SYSTEM,
                    PromptTier::Standard,
                )
            } else {
                Generation::Skipped
            }
            .unwrap_or_record(fallback, &mut model_degraded);
            let component_id = symbol.id.clone();
            let component_label = component_label(&symbol);
            let source_span = SourceSpan::from_symbol(&symbol);
            let purpose = ground_text(
                &generated,
                std::slice::from_ref(&source_span),
                Some(&source_span.citation()),
            );
            SymbolDoc {
                symbol,
                purpose,
                component_id,
                component_label,
                source_span,
            }
        })
        .collect::<Vec<_>>();
    if model_degraded {
        degraded_sources.insert("model-unavailable".to_string());
    }
    let source_excerpt = leading_chunk.map(|chunk| prompts::SourceExcerpt {
        path: file.to_string(),
        line_start: chunk.line_start.max(1),
        line_end: chunk.line_end.max(chunk.line_start.max(1)),
        excerpt: chunk.content.clone(),
    });
    let mut source_spans = symbol_docs
        .iter()
        .map(|symbol| symbol.source_span.clone())
        .collect::<Vec<_>>();
    // Files without indexed symbols (markdown, config, data) ground their
    // content-derived purpose in the leading chunk's line range.
    if source_spans.is_empty()
        && let Some(excerpt) = &source_excerpt
    {
        source_spans.push(SourceSpan {
            file: file.to_string(),
            line_start: excerpt.line_start,
            line_end: excerpt.line_end,
        });
    }
    let prompt_symbols = symbol_docs
        .iter()
        .map(|symbol| prompts::SymbolSummary {
            name: symbol.symbol.qualified_name.clone(),
            kind: symbol.symbol.kind.clone(),
            component_id: symbol.component_id.clone(),
            component_label: symbol.component_label.clone(),
            line_start: symbol.symbol.line_start,
            line_end: symbol.symbol.line_end,
            purpose: symbol.purpose.clone(),
        })
        .collect::<Vec<_>>();
    let component_ids = symbol_docs
        .iter()
        .map(|symbol| symbol.component_id.clone())
        .collect::<Vec<_>>();
    let (summary, body, reused_page) = match reused {
        Some((page, summary)) => (summary, String::new(), Some(page)),
        None => {
            // One aggregate-tier pass produces the file's multi-section
            // narrative body; the one-line summary is derived from it for parent
            // prompts, so a file costs one generation, not two.
            let body = build_file_body(
                file,
                &prompt_symbols,
                source_excerpt.as_slice(),
                &source_spans,
                &symbol_docs,
                generate,
                verify,
                ai_depth,
                &mut degraded_sources,
                &mut verify_notes,
            );
            let summary = file_summary_from_body(&body, file, &symbol_docs);
            (summary, body, None)
        }
    };

    FileDoc {
        path: file.to_string(),
        module,
        summary,
        body,
        source_spans,
        symbols: symbol_docs,
        component_ids,
        degraded: !degraded_sources.is_empty(),
        degraded_sources: degraded_sources.into_iter().collect(),
        verify_notes,
        reused_page,
    }
}

/// Generate the file page's multi-section narrative body (`## Overview` +
/// `## How it fits`) at the aggregate tier, run it through the grounded
/// verification pass, and anchor its citations. Falls back to a deterministic
/// multi-section structural body when generation is off, fails, or yields an
/// unverifiable / empty result; the Key components table is appended by the
/// renderer, not here. A verifier strip degrades the page; an unavailable
/// verifier proceeds undegraded (mirrors the curated-page contract).
#[expect(clippy::too_many_arguments)]
fn build_file_body(
    file: &str,
    prompt_symbols: &[prompts::SymbolSummary],
    sources: &[prompts::SourceExcerpt],
    source_spans: &[SourceSpan],
    symbol_docs: &[SymbolDoc],
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    ai_depth: AiDepth,
    degraded_sources: &mut BTreeSet<String>,
    verify_notes: &mut Vec<VerifyNote>,
) -> String {
    let generated = if ai_depth.includes_files() {
        // Non-code files (no indexed symbols) derive their page from leading
        // content; code files derive it from their symbol evidence. Both render
        // the same multi-section narrative shape at the aggregate tier.
        let (prompt, system) = match (prompt_symbols.is_empty(), sources.first()) {
            (true, Some(excerpt)) => (
                prompts::content_file_prompt(file, excerpt),
                prompts::CONTENT_FILE_SYSTEM,
            ),
            _ => (
                prompts::file_prompt(file, prompt_symbols, sources),
                prompts::FILE_SYSTEM,
            ),
        };
        maybe_generate(generate, &prompt, system, PromptTier::Aggregate)
    } else {
        Generation::Skipped
    };
    let text = match generated {
        Generation::Generated(text) => text,
        Generation::Failed => {
            degraded_sources.insert("model-unavailable".to_string());
            return structural_file_body(file, symbol_docs);
        }
        Generation::Skipped => return structural_file_body(file, symbol_docs),
    };
    let text = match verify_with_notes(verify, &text, prompt_symbols, sources) {
        VerifyOutcome::Skipped => text,
        VerifyOutcome::Verified { text, notes } => {
            verify_notes.extend(notes);
            text
        }
    };
    let citations = citation_list(source_spans, &text);
    let grounded = ground_text(&text, source_spans, Some(&citations));
    if grounded.trim().is_empty() {
        degraded_sources.insert("grounding-empty".to_string());
        return structural_file_body(file, symbol_docs);
    }
    grounded
}

/// Deterministic multi-section file body for `--ai off`, generation failure, or
/// an unverifiable draft: a real `## Overview` and `## How it fits` so the page
/// keeps narrative structure even without a model. The Key components table is
/// appended by the renderer, so two sections here render as three on the page.
fn structural_file_body(file: &str, symbols: &[SymbolDoc]) -> String {
    let mut body = String::new();
    write_section(
        &mut body,
        "Overview",
        &structural_file_summary(file, symbols),
    );
    write_section(
        &mut body,
        "How it fits",
        &format!(
            "`{file}` is documented from its indexed symbols; see the Key components below \
             and the module page for how it connects to sibling files."
        ),
    );
    body
}

/// Derive the one-line file summary from the body's first prose paragraph
/// (skipping `#` headings), so parent module/repo prompts and index listings get
/// a real purpose without a second per-file generation call. Falls back to the
/// structural summary when the body carries no prose.
fn file_summary_from_body(body: &str, file: &str, symbols: &[SymbolDoc]) -> String {
    body.split("\n\n")
        .map(str::trim)
        .find(|block| !block.is_empty() && !block.starts_with('#'))
        .map(str::to_string)
        .unwrap_or_else(|| structural_file_summary(file, symbols))
}
