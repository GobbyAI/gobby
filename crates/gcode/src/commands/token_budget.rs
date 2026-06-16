pub(crate) const TOKEN_ESTIMATE_CHARS_PER_TOKEN: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TokenBudgetTrim<T> {
    pub(crate) results: Vec<T>,
    pub(crate) hint: Option<String>,
}

pub(crate) fn estimate_tokens(text: &str) -> usize {
    if text.is_empty() {
        0
    } else {
        text.chars()
            .count()
            .div_ceil(TOKEN_ESTIMATE_CHARS_PER_TOKEN)
    }
}

pub(crate) fn trim_results<T, F>(
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

    let hint = (kept.len() < total)
        .then(|| token_budget_hint(token_budget, kept.len(), total, refine_with));
    TokenBudgetTrim {
        results: kept,
        hint,
    }
}

pub(crate) fn combine_hints(first: Option<String>, second: Option<String>) -> Option<String> {
    match (first, second) {
        (Some(first), Some(second)) => Some(format!("{first} {second}")),
        (Some(first), None) => Some(first),
        (None, Some(second)) => Some(second),
        (None, None) => None,
    }
}

fn token_budget_hint(token_budget: usize, kept: usize, total: usize, refine_with: &str) -> String {
    format!(
        "Token budget {token_budget} limited output to {kept} of {total} results using ceil(chars/4) row estimates; refine with {refine_with}."
    )
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

    #[test]
    fn trim_results_preserves_priority_prefix_and_hints() {
        let trimmed = trim_results(
            vec!["abcd", "abcdefgh", "zzzz"],
            Some(3),
            "--kind or PATH",
            |row| row.to_string(),
        );

        assert_eq!(trimmed.results, vec!["abcd", "abcdefgh"]);
        let hint = trimmed.hint.expect("budget hint");
        assert!(hint.contains("2 of 3 results"));
        assert!(hint.contains("ceil(chars/4)"));
        assert!(hint.contains("refine with --kind or PATH"));
    }

    #[test]
    fn combine_hints_keeps_both_messages() {
        let combined = combine_hints(Some("first".to_string()), Some("second".to_string()))
            .expect("combined hint");

        assert_eq!(combined, "first second");
    }
}
