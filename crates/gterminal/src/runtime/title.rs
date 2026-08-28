const CLAUDE_ACTIVITY_GLYPHS: &str = "·✢✳✶✻✽";

pub(crate) fn stripped_terminal_title(title: &str) -> Option<String> {
    let title = title.trim();
    if title.is_empty() {
        return None;
    }

    let mut chars = title.char_indices();
    let (_, first) = chars.next()?;
    let after_first = &title[first.len_utf8()..];
    let recognized =
        matches!(first, '\u{2800}'..='\u{28ff}') || CLAUDE_ACTIVITY_GLYPHS.contains(first);
    let stripped = if recognized
        && (after_first.is_empty() || after_first.chars().next().is_some_and(char::is_whitespace))
    {
        after_first.trim()
    } else {
        title
    };

    (!stripped.is_empty()).then(|| stripped.to_string())
}

#[cfg(test)]
#[path = "title/tests.rs"]
mod tests;
