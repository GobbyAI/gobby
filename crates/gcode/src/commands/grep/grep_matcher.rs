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
            anyhow::bail!("gcode grep pattern must not be empty");
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
    let bytes = line.as_bytes();
    let mut token_start = span.start;
    while token_start < span.end && !is_identifier_byte(bytes[token_start]) {
        token_start += 1;
    }
    if token_start == span.end {
        return false;
    }

    let mut token_end = token_start;
    while token_end < span.end && is_identifier_byte(bytes[token_end]) {
        token_end += 1;
    }

    let before_attached = token_start
        .checked_sub(1)
        .is_some_and(|index| is_identifier_byte(bytes[index]));
    let after_attached = bytes
        .get(token_end)
        .is_some_and(|byte| is_identifier_byte(*byte));

    !before_attached && !after_attached
}

fn is_identifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
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
}
