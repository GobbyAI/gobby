//! Generic token-budget estimation and result pagination.
//!
//! Pure, always-compiled logic shared by the Gobby CLIs: estimate the token
//! cost of rendered output with a `ceil(chars / 4)` heuristic, page an ordered
//! result list without splitting semantic items, and combine unrelated hints.
//! `gcode` consumes the pager directly. The legacy trim adapter remains only
//! while the disabled `gwiki` search is replaced by epic #19664.

/// Characters-per-token divisor for the `ceil(chars / 4)` estimate heuristic.
pub const TOKEN_ESTIMATE_CHARS_PER_TOKEN: usize = 4;

/// Outcome of [`paginate_results`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenBudgetPage<T> {
    pub results: Vec<T>,
    pub next_offset: Option<usize>,
    pub budget_exceeded: bool,
}

/// Legacy gwiki-only trimming result retained during epic #19664.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenBudgetTrim<T> {
    pub results: Vec<T>,
    pub hint: Option<String>,
}

/// Estimate the token cost of `text` as `ceil(chars / 4)` (0 for empty input).
pub fn estimate_tokens(text: &str) -> usize {
    if text.is_empty() {
        0
    } else {
        text.chars()
            .count()
            .div_ceil(TOKEN_ESTIMATE_CHARS_PER_TOKEN)
    }
}

/// Page `results` to the largest complete prefix whose fully rendered page fits.
///
/// `results` starts at `offset`; `has_more` reports whether the backing collection
/// has additional items after it. `render_page` must render the complete page,
/// including headers and continuation metadata, so all emitted text is charged
/// against the budget. When one complete item alone exceeds the budget it is
/// returned with `budget_exceeded = true` so callers always retain a retrieval path.
pub fn paginate_results<T, F>(
    results: Vec<T>,
    offset: usize,
    has_more: bool,
    token_budget: Option<usize>,
    render_page: F,
) -> TokenBudgetPage<T>
where
    F: Fn(&[T], Option<usize>, bool) -> String,
{
    let Some(token_budget) = token_budget else {
        let next_offset = has_more.then_some(offset.saturating_add(results.len()));
        return TokenBudgetPage {
            results,
            next_offset,
            budget_exceeded: false,
        };
    };

    if results.is_empty() {
        let budget_exceeded = estimate_tokens(&render_page(&[], None, false)) > token_budget;
        return TokenBudgetPage {
            results,
            next_offset: None,
            budget_exceeded,
        };
    }

    let mut kept = None;
    for count in 1..=results.len() {
        let page_has_more = count < results.len() || has_more;
        let next_offset = page_has_more.then_some(offset.saturating_add(count));
        let rendered = render_page(&results[..count], next_offset, false);
        if estimate_tokens(&rendered) <= token_budget {
            kept = Some(count);
        }
    }

    let (kept, budget_exceeded) = kept.map_or((1, true), |count| (count, false));
    let page_has_more = kept < results.len() || has_more;
    let next_offset = page_has_more.then_some(offset.saturating_add(kept));
    TokenBudgetPage {
        results: results.into_iter().take(kept).collect(),
        next_offset,
        budget_exceeded,
    }
}

/// Legacy gwiki-only row trimmer retained during epic #19664.
pub fn trim_results<T, F>(
    results: Vec<T>,
    token_budget: Option<usize>,
    refine_with: &str,
    render: F,
) -> TokenBudgetTrim<T>
where
    F: Fn(&T) -> String,
{
    let Some(token_budget) = token_budget else {
        return TokenBudgetTrim {
            results,
            hint: None,
        };
    };

    let total = results.len();
    let mut used_tokens = 0usize;
    let mut kept = Vec::with_capacity(total);
    for result in results {
        let estimated_tokens = estimate_tokens(&render(&result));
        if used_tokens.saturating_add(estimated_tokens) > token_budget {
            break;
        }
        used_tokens += estimated_tokens;
        kept.push(result);
    }
    let hint = (kept.len() < total).then(|| {
        format!(
            "Token budget {token_budget} limited output to {} of {total} results using ceil(chars/4) row estimates; refine with {refine_with}.",
            kept.len()
        )
    });
    TokenBudgetTrim {
        results: kept,
        hint,
    }
}

/// Join two optional hints into one space-separated message.
pub fn combine_hints(first: Option<String>, second: Option<String>) -> Option<String> {
    match (first, second) {
        (Some(first), Some(second)) => Some(format!("{first} {second}")),
        (Some(first), None) => Some(first),
        (None, Some(second)) => Some(second),
        (None, None) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_estimate_uses_four_char_ceil() {
        assert_eq!(estimate_tokens(""), 0);
        assert_eq!(estimate_tokens("a"), 1);
        assert_eq!(estimate_tokens("abcd"), 1);
        assert_eq!(estimate_tokens("abcde"), 2);
    }

    fn render_page(rows: &[&str], next_offset: Option<usize>, budget_exceeded: bool) -> String {
        format!(
            "rows={} next={next_offset:?} exceeded={budget_exceeded}",
            rows.join(",")
        )
    }

    #[test]
    fn no_budget_keeps_all_results_and_reports_backing_continuation() {
        let page = paginate_results(vec!["a", "b"], 4, true, None, render_page);

        assert_eq!(page.results, vec!["a", "b"]);
        assert_eq!(page.next_offset, Some(6));
        assert!(!page.budget_exceeded);
    }

    #[test]
    fn exact_fit_keeps_the_complete_page() {
        let expected = render_page(&["a", "b"], None, false);
        let page = paginate_results(
            vec!["a", "b"],
            0,
            false,
            Some(estimate_tokens(&expected)),
            render_page,
        );

        assert_eq!(page.results, vec!["a", "b"]);
        assert_eq!(page.next_offset, None);
        assert!(!page.budget_exceeded);
    }

    #[test]
    fn multiple_pages_preserve_order_without_gaps_or_duplicates() {
        let rows = ["a", "b", "c", "d"];
        let budget = estimate_tokens(&render_page(&rows[..2], Some(2), false));
        let first = paginate_results(rows.to_vec(), 0, false, Some(budget), render_page);
        let next_offset = first.next_offset.expect("continuation");
        let second = paginate_results(
            rows[next_offset..].to_vec(),
            next_offset,
            false,
            Some(budget),
            render_page,
        );

        assert_eq!([first.results, second.results].concat(), rows);
        assert_eq!(second.next_offset, None);
    }

    #[test]
    fn out_of_range_offset_returns_an_empty_final_page() {
        let page = paginate_results(Vec::<&str>::new(), 9, false, Some(20), render_page);

        assert!(page.results.is_empty());
        assert_eq!(page.next_offset, None);
        assert!(!page.budget_exceeded);
    }

    #[test]
    fn oversized_first_item_is_returned_complete() {
        let page = paginate_results(
            vec!["a very large complete item"],
            0,
            false,
            Some(1),
            render_page,
        );

        assert_eq!(page.results, vec!["a very large complete item"]);
        assert!(page.budget_exceeded);
        assert_eq!(page.next_offset, None);
    }

    #[test]
    fn final_render_overhead_is_charged_to_the_budget() {
        let budget = estimate_tokens("a,b");
        let page = paginate_results(vec!["a", "b"], 0, false, Some(budget), render_page);

        assert_eq!(page.results, vec!["a"]);
        assert!(page.budget_exceeded);
        assert_eq!(page.next_offset, Some(1));
    }

    #[test]
    fn continuation_offset_is_deterministic() {
        let page = paginate_results(vec!["a", "b", "c"], 7, true, Some(9), render_page);

        assert_eq!(page.next_offset, Some(7 + page.results.len()));
    }

    #[test]
    fn combine_hints_keeps_both_messages() {
        let combined = combine_hints(Some("first".to_string()), Some("second".to_string()))
            .expect("combined hint");

        assert_eq!(combined, "first second");
    }
}
