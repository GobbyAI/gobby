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
                [
                    module_wikilink(&child.module),
                    // Leading-paragraph gist only; the child's full brief (with
                    // its own tables) lives on the linked child-module page.
                    super::cell_summary(&child.summary),
                ],
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
    // The page leads with the verified narrative body above; the reference table
    // is demoted below it and capped. It carries the file's real API/code
    // symbols (name + kind + hub purpose only) — the agent-facing detail (UUID
    // component IDs, byte/line offsets, signatures) lives in `gcode`, where it
    // stays fresher. Test-gated symbols are collapsed into a single behavior-spec
    // line instead of one row each, so the readable surface is code, not a test
    // roster.
    doc.push_str("## Reference\n\n");

    let (tests, api): (Vec<&SymbolDoc>, Vec<&SymbolDoc>) =
        file.symbols.iter().partition(|symbol| symbol.is_test);

    if api.is_empty() {
        if tests.is_empty() {
            doc.push_str("No indexed symbols.\n");
        } else {
            push_test_summary_line(&mut doc, tests.len());
        }
        return doc;
    }

    write_markdown_table_header(&mut doc, &["Symbol", "Kind", "Purpose"]);
    for symbol in api.iter().take(REFERENCE_ROW_CAP) {
        // Deterministic deprecation badge (#889): a `Some` reason marks this
        // symbol as deprecated. Surface a visible badge in the Symbol cell and
        // carry the reason inline into the Purpose cell so the readable row keeps
        // its 3-column shape.
        let symbol_cell = match &symbol.deprecation {
            Some(_) => format!(
                "{} ⚠️ **deprecated**",
                inline_code(&symbol.symbol.qualified_name)
            ),
            None => inline_code(&symbol.symbol.qualified_name),
        };
        let purpose_cell = match &symbol.deprecation {
            Some(reason) if !reason.is_empty() => {
                let reason = neutralize_symbol_purpose_links(reason);
                format!(
                    "⚠️ **deprecated** — {} {}",
                    reason,
                    neutralize_symbol_purpose_links(&symbol.purpose)
                )
            }
            Some(_) => format!(
                "⚠️ **deprecated** {}",
                neutralize_symbol_purpose_links(&symbol.purpose)
            ),
            None => neutralize_symbol_purpose_links(&symbol.purpose),
        };
        write_markdown_table_row(
            &mut doc,
            [symbol_cell, symbol.symbol.kind.clone(), purpose_cell],
        );
    }
    doc.push('\n');
    if api.len() > REFERENCE_ROW_CAP {
        let _ = writeln!(
            doc,
            "_{} more symbol(s) not shown — run `gcode outline {}` for the full list._\n",
            api.len() - REFERENCE_ROW_CAP,
            file.path
        );
    }
    if !tests.is_empty() {
        push_test_summary_line(&mut doc, tests.len());
    }
    doc
}

/// Max rows in a file page's `## Reference` table. A readable cap so a large
/// file lists its most prominent symbols (in index order) rather than dumping a
/// hundred-row index; the overflow count points the reader at `gcode` for the
/// rest.
const REFERENCE_ROW_CAP: usize = 24;

/// Append the single behavior-spec line that collapses a file's test-gated
/// symbols into a count, so the file page reports its in-file test coverage
/// without listing every test as a `## Reference` row.
fn push_test_summary_line(doc: &mut String, count: usize) {
    let plural = if count == 1 { "" } else { "s" };
    let _ = writeln!(doc, "_Verified by {count} in-file unit test{plural}._\n");
}
