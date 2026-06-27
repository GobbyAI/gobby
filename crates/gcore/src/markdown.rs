//! Markdown normalization shared by generated Gobby documents.

/// Normalize generated Markdown while preserving fenced-code content.
///
/// The formatter collapses multiple blank lines outside fenced code blocks,
/// trims trailing whitespace outside fences, keeps headings and fences
/// surrounded by a single blank line, preserves leading YAML/TOML frontmatter
/// bytes, and leaves nonempty Markdown with exactly one trailing newline.
pub fn normalize_markdown(input: &str) -> String {
    let body_start = frontmatter_body_start(input).unwrap_or(0);
    let prefix = &input[..body_start];
    let inner = normalize_body(&input[body_start..]);

    let mut output = String::with_capacity(input.len() + 1);
    output.push_str(prefix);
    if !inner.is_empty() {
        if !prefix.is_empty() {
            output.push('\n');
        }
        output.push_str(&inner);
    }
    while output.ends_with('\n') {
        output.pop();
    }
    if !output.is_empty() {
        output.push('\n');
    }
    output
}

#[derive(Debug, Clone, Copy)]
struct MarkdownFence {
    marker: u8,
    len: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BodyLine {
    Blank,
    Heading(String),
    FenceOpen(String),
    FenceContent(String),
    FenceClose(String),
    Text(String),
}

fn normalize_body(body: &str) -> String {
    let mut lines = Vec::new();
    let mut fence: Option<MarkdownFence> = None;
    for raw in body.split('\n') {
        if let Some(active) = fence {
            if markdown_fence_closes(raw, active) {
                fence = None;
                lines.push(BodyLine::FenceClose(raw.to_string()));
            } else {
                lines.push(BodyLine::FenceContent(raw.to_string()));
            }
        } else {
            let stripped = raw.trim_end();
            if let Some(opening) = markdown_fence_start(stripped) {
                fence = Some(opening);
                lines.push(BodyLine::FenceOpen(stripped.to_string()));
            } else if parse_atx_heading(stripped).is_some() {
                lines.push(BodyLine::Heading(stripped.to_string()));
            } else if stripped.is_empty() {
                lines.push(BodyLine::Blank);
            } else {
                lines.push(BodyLine::Text(stripped.to_string()));
            }
        }
    }

    let mut out: Vec<String> = Vec::new();
    let mut pending_blank = false;
    for line in lines {
        match line {
            BodyLine::Blank => {
                push_blank(&mut out);
                pending_blank = false;
            }
            BodyLine::Heading(text) => {
                push_blank(&mut out);
                out.push(text);
                pending_blank = true;
            }
            BodyLine::FenceOpen(text) => {
                push_blank(&mut out);
                out.push(text);
                pending_blank = false;
            }
            BodyLine::FenceContent(text) => {
                out.push(text);
                pending_blank = false;
            }
            BodyLine::FenceClose(text) => {
                out.push(text);
                pending_blank = true;
            }
            BodyLine::Text(text) => {
                if pending_blank {
                    push_blank(&mut out);
                }
                out.push(text);
                pending_blank = false;
            }
        }
    }

    while out.last().is_some_and(|line| line.is_empty()) {
        out.pop();
    }
    out.join("\n")
}

fn push_blank(out: &mut Vec<String>) {
    if out.last().is_some_and(|line| !line.is_empty()) {
        out.push(String::new());
    }
}

fn frontmatter_body_start(markdown: &str) -> Option<usize> {
    delimiter_content_start(markdown, "---")
        .and_then(|content_start| find_closing_delimiter(markdown, "---", content_start))
        .or_else(|| {
            delimiter_content_start(markdown, "+++")
                .and_then(|content_start| find_closing_delimiter(markdown, "+++", content_start))
        })
        .map(|(_, body_start)| body_start)
}

fn delimiter_content_start(markdown: &str, marker: &str) -> Option<usize> {
    let rest = markdown.strip_prefix(marker)?;
    if rest.starts_with("\r\n") {
        Some(marker.len() + 2)
    } else if rest.starts_with('\n') {
        Some(marker.len() + 1)
    } else {
        None
    }
}

fn find_closing_delimiter(
    markdown: &str,
    marker: &str,
    mut offset: usize,
) -> Option<(usize, usize)> {
    while offset <= markdown.len() {
        let line_end = markdown[offset..]
            .find('\n')
            .map_or(markdown.len(), |relative| offset + relative);
        let line_content_end = markdown[..line_end]
            .strip_suffix('\r')
            .map_or(line_end, |line| line.len());
        let line = &markdown[offset..line_content_end];

        if line.trim() == marker {
            let body_start = if line_end < markdown.len() {
                line_end + 1
            } else {
                line_end
            };
            return Some((offset, body_start));
        }

        if line_end == markdown.len() {
            break;
        }
        offset = line_end + 1;
    }

    None
}

fn markdown_fence_start(line: &str) -> Option<MarkdownFence> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }
    let trimmed = &line[leading_spaces..];
    let marker = match trimmed.as_bytes().first().copied()? {
        b'`' | b'~' => trimmed.as_bytes()[0],
        _ => return None,
    };
    let len = trimmed.bytes().take_while(|byte| *byte == marker).count();
    (len >= 3).then_some(MarkdownFence { marker, len })
}

fn markdown_fence_closes(line: &str, fence: MarkdownFence) -> bool {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return false;
    }
    let trimmed = &line[leading_spaces..];
    let len = trimmed
        .bytes()
        .take_while(|byte| *byte == fence.marker)
        .count();
    len >= fence.len && trimmed[len..].trim().is_empty()
}

fn parse_atx_heading(line: &str) -> Option<(u8, String)> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }

    let line = &line[leading_spaces..];
    let level = line.bytes().take_while(|byte| *byte == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }

    let after_marks = &line[level..];
    if !after_marks.is_empty() && !after_marks.chars().next().is_some_and(char::is_whitespace) {
        return None;
    }

    let title = strip_atx_closing_sequence(after_marks.trim()).to_string();
    Some((level as u8, title))
}

fn strip_atx_closing_sequence(title: &str) -> &str {
    let mut hash_start = title.len();
    let mut saw_hash = false;
    for (index, ch) in title.char_indices().rev() {
        if ch == '#' {
            saw_hash = true;
            hash_start = index;
        } else {
            break;
        }
    }
    if saw_hash
        && hash_start > 0
        && title[..hash_start]
            .chars()
            .next_back()
            .is_some_and(char::is_whitespace)
    {
        title[..hash_start].trim_end()
    } else {
        title
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_markdown;

    #[test]
    fn markdown_collapses_multiple_blank_lines_outside_fences() {
        assert_eq!(
            normalize_markdown("# A\n\n\ntext\n\n\nmore\n"),
            "# A\n\ntext\n\nmore\n"
        );
    }

    #[test]
    fn markdown_preserves_fenced_code_content_verbatim() {
        let input = "# A\n```rust\nfn main() {\n\n\n}\n```\n\n\ntext\n";
        assert_eq!(
            normalize_markdown(input),
            "# A\n\n```rust\nfn main() {\n\n\n}\n```\n\ntext\n"
        );
    }

    #[test]
    fn markdown_preserves_frontmatter_and_separates_body() {
        let input = "+++\ntitle = \"A\"\n+++\n\n\n# A\n";
        assert_eq!(
            normalize_markdown(input),
            "+++\ntitle = \"A\"\n+++\n\n# A\n"
        );
    }

    #[test]
    fn markdown_surrounds_headings_and_fences() {
        assert_eq!(
            normalize_markdown("intro\n# A\ntext\n```text\nx\n```\nafter\n"),
            "intro\n\n# A\n\ntext\n\n```text\nx\n```\n\nafter\n"
        );
    }

    #[test]
    fn markdown_ends_with_exactly_one_trailing_newline() {
        assert_eq!(normalize_markdown("# A\n\n\n"), "# A\n");
        assert_eq!(normalize_markdown(""), "");
    }

    #[test]
    fn markdown_normalize_is_idempotent() {
        let input = "---\ntitle: A\n---\n\n# A\n\n\n```text\nx\n\n\n```\n\nBody\n";
        let once = normalize_markdown(input);
        assert_eq!(normalize_markdown(&once), once);
    }
}
