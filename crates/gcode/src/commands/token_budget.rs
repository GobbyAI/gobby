//! Token-budget paging and continuation presentation for gcode collections.
//!
//! Whole-item page selection lives in [`gobby_core::token_budget`]. This module
//! owns gcode's automatic budget and shell-safe text continuation contract.

use std::ffi::{OsStr, OsString};

use serde::Serialize;

use crate::output::{self, Format};

pub(crate) const AUTOMATIC_TEXT_TOKEN_BUDGET: usize = 2_000;

#[derive(Clone, Copy)]
pub(crate) struct CollectionPageMeta<'a> {
    pub project_id: &'a str,
    pub total: usize,
    pub offset: usize,
    pub limit: usize,
    pub hint: Option<&'a str>,
}

#[derive(Serialize)]
struct CollectionPage<'a, T: Serialize> {
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
}

pub(crate) fn window<T>(
    results: Vec<T>,
    offset: usize,
    limit: Option<usize>,
) -> (usize, usize, Vec<T>) {
    let total = results.len();
    let limit = limit.unwrap_or_else(|| total.saturating_sub(offset));
    let results = results.into_iter().skip(offset).take(limit).collect();
    (total, limit, results)
}

pub(crate) fn paginate<T, F>(
    results: Vec<T>,
    meta: CollectionPageMeta<'_>,
    token_budget: Option<usize>,
    format: Format,
    render_rows: F,
) -> TokenBudgetPage<T>
where
    T: Serialize,
    F: Fn(&[T]) -> String,
{
    let has_more = meta.total > meta.offset.saturating_add(results.len());
    paginate_results(
        results,
        meta.offset,
        has_more,
        token_budget,
        |rows, next_offset, budget_exceeded| match format {
            Format::Json => render_json_page(meta, rows, next_offset, budget_exceeded),
            Format::Text => render_text_page(&render_rows(rows), next_offset),
        },
    )
}

pub(crate) fn print_page<T, F>(
    page: &TokenBudgetPage<T>,
    meta: CollectionPageMeta<'_>,
    format: Format,
    render_rows: F,
) -> anyhow::Result<()>
where
    T: Serialize,
    F: Fn(&[T]) -> String,
{
    match format {
        Format::Json => {
            print_json_page(meta, &page.results, page.next_offset, page.budget_exceeded)
        }
        Format::Text => {
            let rendered = render_text_page(&render_rows(&page.results), page.next_offset);
            if rendered.is_empty() {
                Ok(())
            } else {
                output::print_text(&rendered)
            }
        }
    }
}

pub(crate) fn render_json_page<T: Serialize>(
    meta: CollectionPageMeta<'_>,
    results: &[T],
    next_offset: Option<usize>,
    budget_exceeded: bool,
) -> String {
    // Collection fields use only derived serializers with string map keys.
    serde_json::to_string(&collection_page(
        meta,
        results,
        next_offset,
        budget_exceeded,
    ))
    .expect("derived collection page serialization cannot fail")
}

pub(crate) fn print_json_page<T: Serialize>(
    meta: CollectionPageMeta<'_>,
    results: &[T],
    next_offset: Option<usize>,
    budget_exceeded: bool,
) -> anyhow::Result<()> {
    output::print_json(&collection_page(
        meta,
        results,
        next_offset,
        budget_exceeded,
    ))
}

fn collection_page<'a, T: Serialize>(
    meta: CollectionPageMeta<'a>,
    results: &'a [T],
    next_offset: Option<usize>,
    budget_exceeded: bool,
) -> CollectionPage<'a, T> {
    CollectionPage {
        project_id: meta.project_id,
        total: meta.total,
        offset: meta.offset,
        limit: meta.limit,
        results,
        next_offset,
        budget_exceeded,
        hint: meta.hint,
    }
}

fn is_false(value: &bool) -> bool {
    !*value
}

pub(crate) fn render_text_page(body: &str, next_offset: Option<usize>) -> String {
    let Some(next_offset) = next_offset else {
        return body.to_string();
    };
    let continuation = continuation_command(next_offset);
    if body.is_empty() {
        format!("continue: {continuation}")
    } else {
        format!("{body}\n\ncontinue: {continuation}")
    }
}

fn continuation_command(next_offset: usize) -> String {
    continuation_command_from(std::env::args_os(), next_offset)
}

fn continuation_command_from(
    args: impl IntoIterator<Item = OsString>,
    next_offset: usize,
) -> String {
    let mut kept = Vec::new();
    let mut args = args.into_iter().peekable();
    let mut inserted = false;

    while let Some(arg) = args.next() {
        if !inserted && arg == OsStr::new("--") {
            kept.push(OsString::from("--offset"));
            kept.push(OsString::from(next_offset.to_string()));
            inserted = true;
            kept.push(arg);
            kept.extend(args);
            break;
        }
        if arg == OsStr::new("--offset") {
            let _ = args.next();
            continue;
        }
        if arg.to_string_lossy().starts_with("--offset=") {
            continue;
        }
        kept.push(arg);
    }

    if !inserted {
        kept.push(OsString::from("--offset"));
        kept.push(OsString::from(next_offset.to_string()));
    }

    kept.iter()
        .map(|arg| shell_quote(arg))
        .collect::<Vec<_>>()
        .join(" ")
}

fn shell_quote(value: &OsStr) -> String {
    let value = value.to_string_lossy();
    if !value.is_empty()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || "_@%+=:,./-".contains(ch))
    {
        value.into_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}

pub(crate) use gobby_core::token_budget::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn continuation_replaces_existing_offset_and_quotes_arguments() {
        let command = continuation_command_from(
            ["gcode", "search", "two words", "--offset=4", "--kind", "fn"]
                .into_iter()
                .map(OsString::from),
            9,
        );

        assert_eq!(command, "gcode search 'two words' --kind fn --offset 9");
    }

    #[test]
    fn continuation_inserts_offset_before_double_dash() {
        let command = continuation_command_from(
            ["gcode", "grep", "needle", "--", "--offset"]
                .into_iter()
                .map(OsString::from),
            3,
        );

        assert_eq!(command, "gcode grep needle --offset 3 -- --offset");
    }

    #[test]
    fn text_page_includes_exact_continuation_metadata() {
        let rendered = render_text_page("result", Some(5));

        assert!(rendered.starts_with("result\n\ncontinue: "));
        assert!(rendered.ends_with("--offset 5"));
    }
}
