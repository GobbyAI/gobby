use anyhow::Context as _;

use super::GrepSpan;

#[derive(Debug)]
pub(super) struct GrepMatcher {
    regex: regex::Regex,
    word: bool,
}

impl GrepMatcher {
    pub(super) fn new(
        pattern: &str,
        fixed_strings: bool,
        ignore_case: bool,
        word: bool,
    ) -> anyhow::Result<Self> {
        if pattern.is_empty() {
            anyhow::bail!("pattern cannot be empty");
        }
        let pattern = if fixed_strings {
            regex::escape(pattern)
        } else {
            pattern.to_string()
        };
        let regex = regex::RegexBuilder::new(&pattern)
            .case_insensitive(ignore_case)
            .build()
            .with_context(|| "invalid gcode grep pattern")?;
        Ok(Self { regex, word })
    }

    pub(super) fn find_spans(&self, line: &str) -> Vec<GrepSpan> {
        self.regex
            .find_iter(line)
            .filter(|m| m.start() != m.end())
            .map(|m| GrepSpan {
                start: m.start(),
                end: m.end(),
            })
            .filter(|span| !self.word || has_identifier_boundaries(line, span))
            .collect()
    }
}

fn has_identifier_boundaries(line: &str, span: &GrepSpan) -> bool {
    let Some((start_offset, first)) = line[span.start..span.end]
        .char_indices()
        .find(|(_, ch)| is_identifier_char(*ch))
    else {
        return has_adjacent_identifier_boundaries(line, span.start, span.end);
    };
    let token_start = span.start + start_offset;
    let mut token_end = token_start + first.len_utf8();
    let remaining_start = token_end;

    for (offset, ch) in line[remaining_start..span.end].char_indices() {
        if !is_identifier_char(ch) {
            break;
        }
        token_end = remaining_start + offset + ch.len_utf8();
    }

    has_adjacent_identifier_boundaries(line, token_start, token_end)
}

fn has_adjacent_identifier_boundaries(line: &str, start: usize, end: usize) -> bool {
    let before_attached = line[..start]
        .chars()
        .next_back()
        .is_some_and(is_identifier_char);
    let after_attached = line[end..].chars().next().is_some_and(is_identifier_char);

    !before_attached && !after_attached
}

// `gcode grep -w` uses ASCII identifier boundaries to match indexed source tokens.
fn is_identifier_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matched_texts<'a>(matcher: &GrepMatcher, line: &'a str) -> Vec<&'a str> {
        matcher
            .find_spans(line)
            .into_iter()
            .map(|span| &line[span.start..span.end])
            .collect()
    }

    #[test]
    fn word_matching_accepts_identifier_boundaries() {
        let matcher = GrepMatcher::new("note_path", false, false, true).expect("matcher");

        assert_eq!(
            matched_texts(
                &matcher,
                "note_path note_path: &note_path note_path.parent()"
            ),
            vec!["note_path", "note_path", "note_path", "note_path"]
        );
    }

    #[test]
    fn word_matching_rejects_attached_identifier_chars() {
        let matcher = GrepMatcher::new("note_path", false, false, true).expect("matcher");

        assert!(
            matcher
                .find_spans("selected_note_paths note_paths xnote_path")
                .is_empty()
        );
    }

    #[test]
    fn word_matching_treats_unicode_as_non_identifier_chars() {
        let matcher = GrepMatcher::new("bar", false, false, true).expect("matcher");

        assert_eq!(
            matched_texts(&matcher, "føøbar barβ _bar bar_ føø bar; β bar"),
            vec!["bar", "bar", "bar", "bar"]
        );
    }

    #[test]
    fn word_matching_accepts_non_identifier_literals_with_clean_adjacent_boundaries() {
        let matcher = GrepMatcher::new("::", true, false, true).expect("matcher");

        assert_eq!(
            matched_texts(&matcher, "value::field :: value ::field value :: field"),
            vec!["::", "::"]
        );
    }

    #[test]
    fn regex_word_boundaries_still_work_without_word_flag() {
        let matcher = GrepMatcher::new(r"\bnote_path\b", false, false, false).expect("matcher");

        assert_eq!(
            matched_texts(&matcher, "selected_note_paths note_path.parent()"),
            vec!["note_path"]
        );
    }

    #[test]
    fn invalid_regex_reports_gcode_grep_pattern_error() {
        let error = GrepMatcher::new("(", false, false, false).expect_err("invalid regex");

        assert!(
            format!("{error:#}").contains("invalid gcode grep pattern"),
            "unexpected error: {error:#}"
        );
    }

    #[test]
    fn empty_pattern_reports_plain_pattern_error() {
        let error = GrepMatcher::new("", false, false, false).expect_err("empty pattern");

        assert_eq!(error.to_string(), "pattern cannot be empty");
    }
}
