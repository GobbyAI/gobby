use std::fmt::Write as _;

use super::super::*;

pub(crate) fn render_module_doc(module: &ModuleDoc) -> String {
    // Range-free frontmatter and no `<details>` provenance wall: module pages
    // are navigation + narrative, not a per-line source surface (#871).
    let mut doc = frontmatter_with_degradation_and_verify_notes_without_ranges(
        &module.module,
        "code_module",
        &module.source_spans,
        &module.degraded_sources,
        &module.verify_notes,
    );
    let _ = writeln!(doc, "# {}\n", module.module);
    match parent_module(&module.module) {
        Some(parent) => {
            let _ = writeln!(doc, "Parent: {}\n", module_wikilink(parent));
        }
        None => doc.push_str("Parent: [[code/repo|Repository Overview]]\n\n"),
    }
    write_section(&mut doc, "Overview", &module.summary);
    if !module.child_modules.is_empty() {
        doc.push_str("## Child Modules\n\n");
        write_markdown_table_header(&mut doc, &["Module", "Summary"]);
        for child in &module.child_modules {
            write_markdown_table_row(
                &mut doc,
                [module_wikilink(&child.module), child.summary.clone()],
            );
        }
        doc.push('\n');
    }
    if !module.direct_files.is_empty() {
        doc.push_str("## Files\n\n");
        write_markdown_table_header(&mut doc, &["File", "Summary"]);
        for file in &module.direct_files {
            write_markdown_table_row(&mut doc, [file_wikilink(&file.path), file.summary.clone()]);
        }
        doc.push('\n');
    }
    // The module's key components are its child modules and files (above); the
    // raw Component-ID (UUID) dump is dropped — that surface is agent-facing and
    // lives in `gcode`, not the human wiki (#871).
    doc
}

pub(crate) fn render_file_doc(file: &FileDoc) -> String {
    // Range-free frontmatter and no `<details>` provenance wall: the file page
    // leads with a verified narrative body, not a machine source surface (#871).
    let mut doc = frontmatter_with_degradation_and_verify_notes_without_ranges(
        &file.path,
        "code_file",
        &file.source_spans,
        &file.degraded_sources,
        &file.verify_notes,
    );
    let _ = writeln!(doc, "# {}\n", file.path);
    if file.module.is_empty() {
        doc.push_str("Module: [[code/repo|Repository Overview]]\n\n");
    } else {
        let _ = writeln!(doc, "Module: {}\n", module_wikilink(&file.module));
    }
    let body = file.body.trim();
    if !body.is_empty() {
        doc.push_str(body);
        doc.push_str("\n\n");
    }
    // Human Key components table: name + hub purpose only. The agent-facing
    // detail (UUID component IDs, byte/line offsets, signatures) lives in
    // `gcode`, where it stays fresher; the wiki keeps the readable surface.
    doc.push_str("## Key components\n\n");
    if file.symbols.is_empty() {
        doc.push_str("No indexed symbols.\n");
        return doc;
    }
    write_markdown_table_header(&mut doc, &["Symbol", "Kind", "Purpose"]);
    for symbol in &file.symbols {
        write_markdown_table_row(
            &mut doc,
            [
                inline_code(&symbol.symbol.qualified_name),
                symbol.symbol.kind.clone(),
                neutralize_symbol_purpose_links(&symbol.purpose),
            ],
        );
    }
    doc.push('\n');
    doc
}
