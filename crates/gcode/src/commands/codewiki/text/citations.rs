use std::collections::BTreeSet;
use std::fmt::Write as _;

use super::sanitize::sanitize_model_markdown_links;
use crate::commands::codewiki::SourceSpan;

const FALLBACK_CITATION_LINE_WIDTH: usize = 240;

/// Hard cap on fallback citations appended when generated prose carries no
/// valid inline citation. Aggregate pages can cover thousands of spans;
/// appending the full set produced megabyte citation walls that re-entered
/// downstream summaries and prompts (#699).
pub(crate) const MAX_FALLBACK_CITATIONS: usize = 5;

/// Representative subset of `spans` for fallback citations: at most
/// [`MAX_FALLBACK_CITATIONS`] entries, preferring one span per distinct file
/// so broad pages cite breadth rather than one file's span run.
/// Extensions whose files are asset/data provenance rather than behavior;
/// they rank behind source files in fallback citations unless they are the
/// only provenance available.
const ASSET_DATA_EXTENSIONS: &[&str] = &[
    "csv", "gif", "ico", "jpeg", "jpg", "json", "lock", "png", "svg", "toml", "tsv", "xml", "yaml",
    "yml",
];

fn is_asset_or_data_file(file: &str) -> bool {
    std::path::Path::new(file)
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            let extension = extension.to_ascii_lowercase();
            ASSET_DATA_EXTENSIONS.contains(&extension.as_str())
        })
}

/// Lowercased alphanumeric tokens of at least three characters, used for
/// lexical-overlap scoring between generated text and span file paths.
fn lexical_tokens(value: &str) -> BTreeSet<String> {
    value
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| token.len() >= 3)
        .map(str::to_ascii_lowercase)
        .collect()
}

fn citation_relevance(text_tokens: &BTreeSet<String>, file: &str) -> usize {
    lexical_tokens(file)
        .iter()
        .filter(|token| text_tokens.contains(*token))
        .count()
}

/// Picks fallback citation spans by relevance to `text`: files whose path
/// tokens overlap the text rank first, asset/data files rank last (still
/// used when they are the sole provenance), and ties keep deterministic
/// path order. Distinct files are preferred before a second span of the
/// same file is taken.
pub(super) fn fallback_spans(spans: &[SourceSpan], text: &str) -> Vec<SourceSpan> {
    let deduped = spans.iter().cloned().collect::<BTreeSet<_>>();
    let text_tokens = lexical_tokens(text);
    let mut ranked_files = deduped
        .iter()
        .map(|span| span.file.as_str())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    ranked_files.sort_by_key(|file| {
        (
            is_asset_or_data_file(file),
            std::cmp::Reverse(citation_relevance(&text_tokens, file)),
            *file,
        )
    });

    let mut picked: Vec<SourceSpan> = Vec::new();
    for file in &ranked_files {
        if picked.len() == MAX_FALLBACK_CITATIONS {
            return picked;
        }
        if let Some(span) = deduped.iter().find(|span| span.file == *file) {
            picked.push(span.clone());
        }
    }
    for file in &ranked_files {
        if picked.len() == MAX_FALLBACK_CITATIONS {
            break;
        }
        for span in deduped.iter().filter(|span| span.file == *file) {
            if picked.len() == MAX_FALLBACK_CITATIONS {
                break;
            }
            if !picked.contains(span) {
                picked.push(span.clone());
            }
        }
    }
    picked
}

pub(crate) fn citation_list(spans: &[SourceSpan], text: &str) -> String {
    fallback_spans(spans, text)
        .into_iter()
        .map(|span| span.citation())
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn wrap_citation_items<I>(items: I, max_line_width: usize) -> String
where
    I: IntoIterator<Item = String>,
{
    let mut lines = Vec::new();
    let mut line = String::new();
    for item in items {
        let separator_width = usize::from(!line.is_empty());
        if !line.is_empty() && line.len() + separator_width + item.len() > max_line_width {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(&item);
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines.join("\n")
}

pub(crate) fn citation_markers(spans: &[SourceSpan], text: &str) -> String {
    let fallback = fallback_spans(spans, text)
        .into_iter()
        .map(|span| span.citation())
        .collect::<BTreeSet<_>>();
    wrap_citation_items(
        citation_references(spans)
            .into_iter()
            .filter(|(_, citation)| fallback.contains(citation))
            .map(|(index, _)| format!("[{index}]")),
        FALLBACK_CITATION_LINE_WIDTH,
    )
}

fn citation_references(spans: &[SourceSpan]) -> Vec<(usize, String)> {
    spans
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .enumerate()
        .map(|(index, span)| (index + 1, span.citation()))
        .collect()
}

pub(crate) fn replace_citations_with_markers(text: &str, spans: &[SourceSpan]) -> String {
    let mut marked = text.to_string();
    for (index, citation) in citation_references(spans) {
        marked = marked.replace(&citation, &format!("[{index}]"));
    }
    marked
}

/// Appends a References section resolving only the `[N]` markers that appear
/// in `doc`; unreferenced spans stay out so the section scales with the prose
/// rather than with everything the page covers (#699).
pub(crate) fn write_references(doc: &mut String, spans: &[SourceSpan]) {
    let references = citation_references(spans)
        .into_iter()
        .filter(|(index, _)| doc.contains(&format!("[{index}]")))
        .collect::<Vec<_>>();
    if references.is_empty() {
        return;
    }
    doc.push_str("## References\n\n");
    for (index, citation) in references {
        let _ = writeln!(doc, "- [{index}] {citation}");
    }
    doc.push('\n');
}

pub(crate) fn ground_text(
    text: &str,
    valid_spans: &[SourceSpan],
    fallback_citation: Option<&str>,
) -> String {
    let cleaned = sanitize_model_markdown_links(&strip_invalid_citations(text, valid_spans));
    match fallback_citation {
        Some(fallback_citation) if !contains_valid_citation(&cleaned, valid_spans) => {
            if fallback_citation.contains('\n') {
                format!("{cleaned}\n{fallback_citation}")
            } else {
                format!("{cleaned} {fallback_citation}")
            }
        }
        _ => cleaned,
    }
}

fn strip_invalid_citations(text: &str, valid_spans: &[SourceSpan]) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let (before, after_open) = rest.split_at(open);
        out.push_str(before);
        let after_open = &after_open[1..];
        let Some(close) = after_open.find(']') else {
            out.push('[');
            out.push_str(after_open);
            return out;
        };
        let candidate = &after_open[..close];
        if citation_parts(candidate).is_none_or(|(file, start, end)| {
            valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        }) {
            out.push('[');
            out.push_str(candidate);
            out.push(']');
        }
        rest = &after_open[close + 1..];
    }
    out.push_str(rest);
    out
}

fn contains_valid_citation(text: &str, valid_spans: &[SourceSpan]) -> bool {
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let after_open = &rest[open + 1..];
        let Some(close) = after_open.find(']') else {
            return false;
        };
        if let Some((file, start, end)) = citation_parts(&after_open[..close])
            && valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        {
            return true;
        }
        rest = &after_open[close + 1..];
    }
    false
}

fn citation_parts(value: &str) -> Option<(&str, usize, usize)> {
    let (file, range) = value.rsplit_once(':')?;
    if file.is_empty() || file.chars().any(char::is_whitespace) {
        return None;
    }
    let (line_start, line_end) = match range.split_once('-') {
        Some((start, end)) => (start.parse().ok()?, end.parse().ok()?),
        None => {
            let line = range.parse().ok()?;
            (line, line)
        }
    };
    (line_start > 0 && line_start <= line_end).then_some((file, line_start, line_end))
}
