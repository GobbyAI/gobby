use super::super::{
    AiDepth, CodewikiProgress, FileDoc, SourceSpan, SymbolDoc, TextGenerator, citation_list,
    component_label, ground_text, maybe_generate, prompts, structural_file_summary,
    structural_symbol_purpose,
};
use crate::models::Symbol;

/// 1-based position of a file within the generation run, for progress output.
#[derive(Clone, Copy, Debug)]
pub(crate) struct FileDocPosition {
    pub(crate) index: usize,
    pub(crate) total: usize,
}

pub(crate) fn build_file_doc(
    file: &str,
    module: String,
    symbols: Vec<Symbol>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    position: FileDocPosition,
) -> FileDoc {
    let file_verb = if ai_depth.includes_files() {
        "generating"
    } else {
        "building"
    };
    progress.emit(format!(
        "{file_verb} file doc file {}/{} {}",
        position.index, position.total, file
    ));
    let symbol_total = symbols.len();
    let symbol_docs = symbols
        .into_iter()
        .enumerate()
        .map(|(index, symbol)| {
            let fallback = structural_symbol_purpose(&symbol);
            let generated = if ai_depth.includes_symbols() {
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
                )
            } else {
                None
            }
            .unwrap_or(fallback);
            let component_id = symbol.id.clone();
            let component_label = component_label(&symbol);
            let source_span = SourceSpan::from_symbol(&symbol);
            let purpose = ground_text(
                &generated,
                std::slice::from_ref(&source_span),
                &source_span.citation(),
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
    let source_spans = symbol_docs
        .iter()
        .map(|symbol| symbol.source_span.clone())
        .collect::<Vec<_>>();
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
    let generated = if ai_depth.includes_files() {
        maybe_generate(
            generate,
            &prompts::file_prompt(file, &prompt_symbols),
            prompts::FILE_SYSTEM,
        )
    } else {
        None
    }
    .unwrap_or(fallback);
    let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

    FileDoc {
        path: file.to_string(),
        module,
        summary,
        source_spans,
        symbols: symbol_docs,
        component_ids,
    }
}
