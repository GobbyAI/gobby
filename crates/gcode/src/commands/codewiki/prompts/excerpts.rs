use std::fmt::Write as _;

use super::types::SourceExcerpt;

pub(super) fn append_source_excerpt_section(prompt: &mut String, sources: &[SourceExcerpt]) {
    append_source_excerpt_section_n(prompt, sources, MAX_PROMPT_SOURCE_EXCERPTS);
}

/// Like [`append_source_excerpt_section`] but with a caller-chosen excerpt
/// count, so per-page content passes can feed more grounded source than the
/// shared aggregate prompts without changing the aggregate budget.
pub(super) fn append_source_excerpt_section_n(
    prompt: &mut String,
    sources: &[SourceExcerpt],
    take: usize,
) {
    prompt.push_str("\nSource excerpts:\n");
    if sources.is_empty() {
        prompt.push_str("- No source excerpts.\n");
        return;
    }
    for source in sources.iter().take(take) {
        let _ = writeln!(
            prompt,
            "--- {} (lines {}-{})",
            source.path, source.line_start, source.line_end
        );
        prompt.push_str(&bounded_excerpt(&source.excerpt));
        prompt.push('\n');
    }
}

/// Hard cap on one child summary embedded in an aggregate prompt. Aggregate
/// prompts roll up every child; unbounded summaries (citation walls, echoed
/// prompts recorded as summaries) once compounded up the module tree past
/// provider input limits (#698).
pub(super) const CHILD_SUMMARY_EXCERPT_MAX_CHARS: usize = 2_000;

/// Hard cap on one retrieved source excerpt embedded in a prompt, and on how
/// many excerpts a single prompt may carry. Together with
/// [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`] these keep aggregate prompts bounded
/// even though they now carry real source content.
pub(crate) const SOURCE_EXCERPT_MAX_CHARS: usize = 2_400;
pub(crate) const MAX_PROMPT_SOURCE_EXCERPTS: usize = 4;
/// Per-page content-pass excerpt budgets. Curated concept/narrative pages run a
/// dedicated second pass that is fed more source than the shared aggregate
/// prompts, so "rich input -> rich output" holds for the human-facing layer
/// without loosening the four pinned aggregate-prompt tests
/// ([`MAX_PROMPT_SOURCE_EXCERPTS`] stays at 4).
pub(crate) const CONCEPT_PAGE_SOURCE_EXCERPTS: usize = 8;
pub(crate) const NARRATIVE_PAGE_SOURCE_EXCERPTS: usize = 8;
/// Source-excerpt budget for the verification prompt: the auditor sees the same
/// breadth of cited evidence the page was generated against, so a claim grounded
/// in any fed excerpt is judged supported.
pub(crate) const VERIFY_SOURCE_EXCERPTS: usize = 8;

/// First paragraph of a child summary, flattened to one line and hard-capped
/// at [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`], so each prompt list entry stays one
/// bounded line regardless of what an earlier run recorded as the summary.
pub(super) fn summary_excerpt(summary: &str) -> String {
    let paragraph = summary
        .trim()
        .split("\n\n")
        .next()
        .unwrap_or_default()
        .trim();
    let flattened = paragraph.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut excerpt = flattened;
    if excerpt.chars().count() > CHILD_SUMMARY_EXCERPT_MAX_CHARS {
        let body_cap = CHILD_SUMMARY_EXCERPT_MAX_CHARS.saturating_sub(1);
        let cap = excerpt
            .char_indices()
            .nth(body_cap)
            .map(|(index, _)| index)
            .unwrap_or(excerpt.len());
        excerpt.truncate(cap);
        excerpt.push('…');
    }
    excerpt
}

/// Source excerpt text hard-capped at [`SOURCE_EXCERPT_MAX_CHARS`]; newlines
/// are preserved because excerpts carry real source content.
pub(super) fn bounded_excerpt(excerpt: &str) -> String {
    let trimmed = excerpt.trim_end();
    let cap = trimmed
        .char_indices()
        .nth(SOURCE_EXCERPT_MAX_CHARS)
        .map(|(index, _)| index);
    match cap {
        Some(cap) => {
            let mut bounded = trimmed[..cap].to_string();
            bounded.push('…');
            bounded
        }
        None => trimmed.to_string(),
    }
}
