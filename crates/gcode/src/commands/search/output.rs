use serde::Serialize;

use crate::commands::token_budget;
use crate::config::Context;
use crate::models::{SearchResult, SearchWarning};
use crate::output::{self, Format};

#[derive(Clone, Copy)]
pub(super) struct SearchPageMeta<'a> {
    pub project_id: &'a str,
    pub total: usize,
    pub offset: usize,
    pub limit: usize,
    pub hint: Option<&'a str>,
    pub warnings: &'a [SearchWarning],
}

#[derive(Serialize)]
struct SearchPage<'a, T: Serialize> {
    project_id: &'a str,
    total: usize,
    offset: usize,
    limit: usize,
    results: &'a [T],
    #[serde(skip_serializing_if = "Option::is_none")]
    next_offset: Option<usize>,
    #[serde(skip_serializing_if = "is_false")]
    budget_exceeded: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    hint: Option<&'a str>,
    #[serde(skip_serializing_if = "<[SearchWarning]>::is_empty")]
    warnings: &'a [SearchWarning],
}

pub(super) fn paginate<T, F>(
    results: Vec<T>,
    meta: SearchPageMeta<'_>,
    token_budget: Option<usize>,
    format: Format,
    render_rows: F,
) -> token_budget::TokenBudgetPage<T>
where
    T: Serialize,
    F: Fn(&[T]) -> String,
{
    let has_more = meta.total > meta.offset.saturating_add(results.len());
    token_budget::paginate_results(
        results,
        meta.offset,
        has_more,
        token_budget,
        |rows, next_offset, budget_exceeded| match format {
            Format::Json => {
                // Search page fields use only derived serializers with string map keys.
                serde_json::to_string(&page(meta, rows, next_offset, budget_exceeded))
                    .expect("derived search page serialization cannot fail")
            }
            Format::Text => token_budget::render_text_page(&render_rows(rows), next_offset),
        },
    )
}

pub(super) fn print_page<T, F>(
    page_result: &token_budget::TokenBudgetPage<T>,
    meta: SearchPageMeta<'_>,
    format: Format,
    render_rows: F,
) -> anyhow::Result<()>
where
    T: Serialize,
    F: Fn(&[T]) -> String,
{
    match format {
        Format::Json => output::print_json(&page(
            meta,
            &page_result.results,
            page_result.next_offset,
            page_result.budget_exceeded,
        )),
        Format::Text => {
            let rendered = token_budget::render_text_page(
                &render_rows(&page_result.results),
                page_result.next_offset,
            );
            if rendered.is_empty() {
                Ok(())
            } else {
                output::print_text(&rendered)
            }
        }
    }
}

fn page<'a, T: Serialize>(
    meta: SearchPageMeta<'a>,
    results: &'a [T],
    next_offset: Option<usize>,
    budget_exceeded: bool,
) -> SearchPage<'a, T> {
    SearchPage {
        project_id: meta.project_id,
        total: meta.total,
        offset: meta.offset,
        limit: meta.limit,
        results,
        next_offset,
        budget_exceeded,
        hint: meta.hint,
        warnings: meta.warnings,
    }
}

fn is_false(value: &bool) -> bool {
    !*value
}

pub(super) fn render_search_results(results: &[SearchResult], verbose: bool) -> String {
    results
        .iter()
        .map(|result| format_search_result_line(result, verbose))
        .collect::<Vec<_>>()
        .join("\n")
}

fn format_search_result_line(result: &SearchResult, verbose: bool) -> String {
    let base = format!(
        "{}:{} [{}] {}",
        result.file_path, result.line_start, result.kind, result.qualified_name
    );
    if !verbose {
        return base;
    }
    let sources = result
        .sources
        .as_ref()
        .map(|sources| sources.join("+"))
        .unwrap_or_default();
    format!(
        "{base} id={} score={:.4} via={sources}",
        result.id, result.score
    )
}

pub(super) fn render_exact_results(results: &[SearchResult], verbose: bool) -> String {
    results
        .iter()
        .map(|result| {
            let mut line = format!(
                "{}:{}-{} [{}] {}",
                result.file_path,
                result.line_start,
                result.line_end,
                result.kind,
                result.qualified_name
            );
            if verbose {
                line.push_str(&format!(" id={} score={:.4}", result.id, result.score));
                if let Some(sources) = &result.sources {
                    line.push_str(" via=");
                    line.push_str(&sources.join("+"));
                }
            }
            if let Some(signature) = result
                .signature
                .as_deref()
                .filter(|value| !value.is_empty())
            {
                line.push_str(" sig=");
                line.push_str(signature);
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn render_content_results(results: &[crate::models::ContentSearchHit]) -> String {
    results
        .iter()
        .map(|result| {
            format!(
                "{}:{}-{} {}",
                result.file_path,
                result.line_start,
                result.line_end,
                compact_snippet(&result.snippet)
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn compact_snippet(snippet: &str) -> String {
    snippet.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(super) fn print_search_warning(ctx: &Context, verbose: bool, hint: Option<&str>) {
    if verbose
        && !ctx.quiet
        && let Some(hint) = hint
    {
        eprintln!("Hint: {hint}");
    }
}

pub(super) fn print_search_hint(ctx: &Context, is_empty: bool, hint: Option<&str>) {
    if let Some(hint) = search_hint_text(ctx.quiet, is_empty, hint) {
        eprintln!("Hint: {hint}");
    }
}

fn search_hint_text(quiet: bool, is_empty: bool, hint: Option<&str>) -> Option<&str> {
    (!quiet && !is_empty).then_some(hint).flatten()
}

pub(super) fn print_empty_diagnostic(
    ctx: &Context,
    is_empty: bool,
    offset: usize,
    total: usize,
    hint: Option<&str>,
) {
    let has_identity = crate::project::has_identity_file(&ctx.project_root);
    if let Some(diagnostic) =
        empty_diagnostic_text(is_empty, ctx.quiet, has_identity, offset, total, hint)
    {
        eprintln!("{diagnostic}");
    }
}

fn empty_diagnostic_text(
    is_empty: bool,
    quiet: bool,
    has_identity: bool,
    offset: usize,
    total: usize,
    hint: Option<&str>,
) -> Option<String> {
    if !is_empty || quiet {
        return None;
    }
    if offset == 0 && !has_identity {
        return Some("No index found for this project. Run `gcode index` first.".to_string());
    }

    let mut diagnostic = if offset > 0 {
        format!("No results at offset {offset} (total {total})")
    } else {
        "No results.".to_string()
    };
    if let Some(hint) = hint {
        diagnostic.push_str("\nHint: ");
        diagnostic.push_str(hint);
    }
    Some(diagnostic)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_search_omits_ids_scores_and_lanes() {
        let result = SearchResult {
            id: "symbol-id".to_string(),
            name: "run".to_string(),
            qualified_name: "module::run".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            file_path: "src/lib.rs".to_string(),
            line_start: 7,
            line_end: 9,
            score: 0.9,
            rrf_score: Some(0.1),
            summary: None,
            signature: None,
            sources: Some(vec!["fts".to_string()]),
        };

        let compact = render_search_results(std::slice::from_ref(&result), false);
        let verbose = render_search_results(&[result], true);

        assert_eq!(compact, "src/lib.rs:7 [function] module::run");
        assert!(verbose.contains("id=symbol-id"));
        assert!(verbose.contains("score=0.9000"));
        assert!(verbose.contains("via=fts"));
    }

    #[test]
    fn empty_diagnostic_honors_quiet_and_appends_actionable_hint() {
        assert_eq!(
            empty_diagnostic_text(true, false, true, 20, 3, Some("switch lanes")),
            Some("No results at offset 20 (total 3)\nHint: switch lanes".to_string())
        );
        assert_eq!(
            empty_diagnostic_text(true, true, true, 20, 3, Some("switch lanes")),
            None
        );
    }

    #[test]
    fn json_page_keeps_existing_hint_field() {
        let meta = SearchPageMeta {
            project_id: "project-id",
            total: 0,
            offset: 0,
            limit: 10,
            hint: Some("use gcode search-content"),
            warnings: &[],
        };

        let value = serde_json::to_value(page::<SearchResult>(meta, &[], None, false))
            .expect("serialize search page");
        assert_eq!(value["hint"], "use gcode search-content");
    }

    #[test]
    fn non_empty_text_hint_is_visible_unless_quiet() {
        assert_eq!(
            search_hint_text(false, false, Some("use gcode grep -w")),
            Some("use gcode grep -w")
        );
        assert_eq!(
            search_hint_text(true, false, Some("use gcode grep -w")),
            None
        );
        assert_eq!(
            search_hint_text(false, true, Some("use gcode grep -w")),
            None
        );
    }
}
