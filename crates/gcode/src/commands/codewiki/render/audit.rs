use std::fmt::Write as _;

use super::super::*;

/// Render the deterministic deprecations aggregate page (#889). Lists every
/// deprecated symbol grouped by file; renders a clear "none found" line when
/// empty. Never degrades.
pub(crate) fn render_deprecations_doc(doc: &DeprecationsDoc) -> String {
    let mut out = frontmatter_with_degradation_without_ranges(
        "Deprecations",
        "code_deprecations",
        &[],
        &doc.degraded_sources,
    );
    out.push_str("# Deprecations\n\n");
    out.push_str(
        "This page is derived deterministically from a source scan of the documented files \
         — no LLM. A symbol is listed when a `#[deprecated]` attribute or a `DEPRECATED` \
         doc-comment sits directly above its definition (or in its docstring). Each row is the \
         symbol that carries the marker, where it lives, and the recorded reason.\n\n",
    );

    if doc.symbols.is_empty() {
        out.push_str("No deprecated symbols found.\n");
        return out;
    }

    // Group by file in the already-sorted order.
    let mut current_file: Option<&str> = None;
    for symbol in &doc.symbols {
        if current_file != Some(symbol.file.as_str()) {
            if current_file.is_some() {
                out.push('\n');
            }
            let _ = writeln!(out, "## {}\n", file_wikilink(&symbol.file));
            write_markdown_table_header(&mut out, &["Symbol", "Kind", "Location", "Reason"]);
            current_file = Some(symbol.file.as_str());
        }
        let location = format!("{}:{}", symbol.file, symbol.line);
        let reason = if symbol.reason.is_empty() {
            "deprecated".to_string()
        } else {
            symbol.reason.clone()
        };
        write_markdown_table_row(
            &mut out,
            [
                inline_code(&symbol.name),
                symbol.kind.clone(),
                inline_code(&location),
                reason,
            ],
        );
    }
    out.push('\n');
    out
}
