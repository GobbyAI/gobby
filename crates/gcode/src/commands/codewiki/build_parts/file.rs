use std::collections::BTreeSet;

use super::super::{
    AiDepth, CodewikiProgress, FileDoc, Generation, LeadingChunk, PromptTier, ReusePlan,
    SourceSpan, SymbolDoc, TextGenerator, citation_list, component_label, file_doc_path,
    ground_text, maybe_generate, prompts, structural_file_summary, structural_symbol_purpose,
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
    let mut degraded = false;
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
            .unwrap_or_record(fallback, &mut degraded);
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
    let fallback = structural_file_summary(file, &symbol_docs);
    let (summary, reused_page) = match reused {
        Some((page, summary)) => (summary, Some(page)),
        None => {
            let generated = if ai_depth.includes_files() {
                // Non-code files have no symbols to summarize; their purpose
                // comes from the leading content instead. The structural
                // fallback is unchanged either way.
                let (prompt, system) = match (&prompt_symbols[..], &source_excerpt) {
                    ([], Some(excerpt)) => (
                        prompts::content_file_prompt(file, excerpt),
                        prompts::CONTENT_FILE_SYSTEM,
                    ),
                    _ => (
                        prompts::file_prompt(file, &prompt_symbols, source_excerpt.as_slice()),
                        prompts::FILE_SYSTEM,
                    ),
                };
                maybe_generate(generate, &prompt, system, PromptTier::Standard)
            } else {
                Generation::Skipped
            }
            .unwrap_or_record(fallback, &mut degraded);
            let citations = citation_list(&source_spans, &generated);
            let summary = ground_text(&generated, &source_spans, Some(&citations));
            (summary, None)
        }
    };

    FileDoc {
        path: file.to_string(),
        module,
        summary,
        source_spans,
        symbols: symbol_docs,
        component_ids,
        degraded,
        reused_page,
    }
}
