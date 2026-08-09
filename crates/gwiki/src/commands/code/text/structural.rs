use std::collections::BTreeSet;
use std::fmt::Write as _;

use crate::commands::code::Symbol;
use crate::commands::code::{FileLink, ModuleLink, SourceSpan, SymbolDoc, plural};

pub(crate) fn structural_symbol_purpose(symbol: &Symbol) -> String {
    if let Some(summary) = symbol.summary.as_deref().filter(|value| !value.is_empty()) {
        return summary.to_string();
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        return docstring.to_string();
    }
    format!(
        "Indexed {} `{}` in `{}`.",
        symbol.kind, symbol.qualified_name, symbol.file_path
    )
}

pub(crate) fn structural_file_summary(file: &str, symbols: &[SymbolDoc]) -> String {
    if symbols.is_empty() {
        return format!("`{file}` has no indexed API symbols.");
    }
    format!(
        "`{file}` exposes {} indexed API symbol{}.",
        symbols.len(),
        plural(symbols.len())
    )
}

/// Display form of a child summary for overview listings: the structural
/// no-symbol filler returns `None` so index pages list the file link without
/// stub prose.
pub(crate) fn display_child_summary(summary: &str, file: &str) -> Option<String> {
    let filler = structural_file_summary(file, &[]);
    let summary = summary.trim();
    (!summary.is_empty() && summary != filler).then(|| summary.to_string())
}

pub(crate) fn structural_module_summary(
    module: &str,
    files: &[FileLink],
    child_modules: &[ModuleLink],
) -> String {
    let file_count = files.len();
    let child_count = child_modules.len();
    format!(
        "`{module}` contains {file_count} direct file{} and {child_count} child module{}.",
        plural(file_count),
        plural(child_count)
    )
}

pub(crate) fn structural_repo_summary(file_count: usize, module_count: usize) -> String {
    format!(
        "Repository code documentation covers {file_count} file{} across {module_count} module{}.",
        plural(file_count),
        plural(module_count)
    )
}

pub(crate) fn write_section(doc: &mut String, heading: &str, body: &str) {
    let _ = writeln!(doc, "## {heading}\n\n{}\n", body.trim());
}

pub(crate) fn collect_link_spans(files: &[FileLink], modules: &[ModuleLink]) -> Vec<SourceSpan> {
    let mut spans = BTreeSet::new();
    for file in files {
        spans.extend(file.source_spans.iter().cloned());
    }
    for module in modules {
        spans.extend(module.source_spans.iter().cloned());
    }
    spans.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::display_child_summary;

    #[test]
    fn display_child_summary_omits_empty_and_filler_text() {
        assert_eq!(display_child_summary("   ", "src/lib.rs"), None);
        assert_eq!(
            display_child_summary("`src/lib.rs` has no indexed API symbols.", "src/lib.rs"),
            None
        );
        assert_eq!(
            display_child_summary("Useful summary", "src/lib.rs").as_deref(),
            Some("Useful summary")
        );
    }
}
