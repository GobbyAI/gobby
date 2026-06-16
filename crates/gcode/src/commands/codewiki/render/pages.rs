use std::fmt::Write as _;

use super::super::*;
use super::model_degraded_sources;

pub(crate) fn render_module_doc(module: &ModuleDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        &module.module,
        "code_module",
        &module.source_spans,
        &model_degraded_sources(module.degraded),
    );
    append_relevant_source_files(&mut doc, &module.source_spans);
    let _ = writeln!(doc, "# {}\n", module.module);
    match parent_module(&module.module) {
        Some(parent) => {
            let _ = writeln!(doc, "Parent: {}\n", module_wikilink(parent));
        }
        None => doc.push_str("Parent: [[code/repo|Repository Overview]]\n\n"),
    }
    write_section(&mut doc, "Overview", &module.summary);
    match module.graph_availability {
        CodewikiGraphAvailability::Unavailable => {
            doc.push_str("## Dependency Diagram\n\n`degraded: graph-unavailable`\n\n");
        }
        CodewikiGraphAvailability::Available | CodewikiGraphAvailability::Truncated => {
            if module.graph_availability == CodewikiGraphAvailability::Truncated {
                doc.push_str("## Dependency Diagram\n\n`degraded: graph-truncated`\n\n");
            }
            if let Some(diagram) = &module.dependency_diagram {
                if module.graph_availability == CodewikiGraphAvailability::Available {
                    doc.push_str("## Dependency Diagram\n\n");
                }
                doc.push_str(diagram);
                doc.push('\n');
            }
            if let Some(diagram) = &module.call_diagram {
                doc.push_str("## Call Diagram\n\n");
                doc.push_str(diagram);
                doc.push('\n');
            }
        }
    }
    if !module.child_modules.is_empty() {
        doc.push_str("## Child Modules\n\n");
        for child in &module.child_modules {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&child.module),
                child.summary
            );
        }
        doc.push('\n');
    }
    if !module.direct_files.is_empty() {
        doc.push_str("## Files\n\n");
        for file in &module.direct_files {
            let _ = writeln!(doc, "- {} - {}", file_wikilink(&file.path), file.summary);
        }
        doc.push('\n');
    }
    if !module.component_ids.is_empty() {
        doc.push_str("## Components\n\n");
        for component_id in &module.component_ids {
            let _ = writeln!(doc, "- {}", inline_code(component_id));
        }
        doc.push('\n');
    }
    doc
}

pub(crate) fn render_file_doc(file: &FileDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        &file.path,
        "code_file",
        &file.source_spans,
        &model_degraded_sources(file.degraded),
    );
    append_relevant_source_files(&mut doc, &file.source_spans);
    let _ = writeln!(doc, "# {}\n", file.path);
    if file.module.is_empty() {
        doc.push_str("Module: [[code/repo|Repository Overview]]\n\n");
    } else {
        let _ = writeln!(doc, "Module: {}\n", module_wikilink(&file.module));
    }
    write_section(&mut doc, "Purpose", &file.summary);
    doc.push_str("## API Symbols\n\n");
    if file.symbols.is_empty() {
        doc.push_str("No indexed symbols.\n");
        return doc;
    }
    for symbol in &file.symbols {
        let _ = writeln!(
            doc,
            "- {} ({}) component {} ({}) lines {}-{} {}",
            inline_code(&symbol.symbol.qualified_name),
            symbol.symbol.kind,
            inline_code(&symbol.component_label),
            inline_code(&symbol.component_id),
            symbol.symbol.line_start,
            symbol.symbol.line_end,
            symbol.source_span.citation()
        );
        if let Some(signature) = symbol
            .symbol
            .signature
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let _ = writeln!(doc, "  - Signature: {}", inline_code(signature));
        }
        let _ = writeln!(
            doc,
            "  - Purpose: {}",
            neutralize_symbol_purpose_links(&symbol.purpose)
        );
    }
    doc.push('\n');
    doc
}
