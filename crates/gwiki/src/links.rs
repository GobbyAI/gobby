use std::collections::BTreeSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkKind {
    Wikilink,
    Markdown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiLink {
    pub kind: LinkKind,
    pub target: String,
    pub normalized_target: String,
    pub anchor: Option<String>,
    pub alias: Option<String>,
    pub byte_start: usize,
    pub byte_end: usize,
    pub resolved: bool,
}

pub fn normalize_wiki_path(target: &str) -> String {
    normalized_target_parts(target).0
}

pub fn extract_links<I, S>(markdown: &str, known_targets: I) -> Vec<WikiLink>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let known_targets = normalized_targets(known_targets);
    let code_ranges = markdown_code_ranges(markdown);
    let mut links = Vec::new();
    let mut offset = 0;

    while offset < markdown.len() {
        if let Some(next_offset) = code_range_end_containing(&code_ranges, offset) {
            offset = next_offset;
            continue;
        }

        let rest = &markdown[offset..];
        if rest.starts_with("[[") {
            if let Some((link, next_offset)) = parse_wikilink(markdown, offset, &known_targets) {
                links.push(link);
                offset = next_offset;
                continue;
            }
        } else if rest.starts_with('[')
            && !is_image_marker(markdown, offset)
            && let Some((link, next_offset)) = parse_markdown_link(markdown, offset, &known_targets)
        {
            links.push(link);
            offset = next_offset;
            continue;
        }

        offset += next_char_len(markdown, offset);
    }

    links
}

pub fn normalized_targets<I, S>(targets: I) -> BTreeSet<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    targets
        .into_iter()
        .map(|target| normalize_wiki_path(target.as_ref()))
        .collect()
}

fn parse_wikilink(
    markdown: &str,
    byte_start: usize,
    known_targets: &BTreeSet<String>,
) -> Option<(WikiLink, usize)> {
    let inner_start = byte_start + 2;
    let close_start = wikilink_close_start(markdown, inner_start)?;
    let byte_end = close_start + 2;
    let inner = &markdown[inner_start..close_start];
    let (target, alias) = split_alias(inner, '|');
    let target = target.trim();
    if target.is_empty() {
        return None;
    }

    let (normalized_target, anchor) = normalized_target_parts(target);
    let resolved = known_targets.contains(&normalized_target);
    Some((
        WikiLink {
            kind: LinkKind::Wikilink,
            target: target.to_string(),
            normalized_target,
            anchor,
            alias,
            byte_start,
            byte_end,
            resolved,
        },
        byte_end,
    ))
}

fn parse_markdown_link(
    markdown: &str,
    byte_start: usize,
    known_targets: &BTreeSet<String>,
) -> Option<(WikiLink, usize)> {
    let label_start = byte_start + 1;
    let label_end = markdown_label_end(markdown, label_start)?;
    let open_paren = label_end + 1;
    if markdown.as_bytes().get(open_paren).copied() != Some(b'(') {
        return None;
    }

    let destination_start = open_paren + 1;
    let destination_end = markdown_destination_end(markdown, destination_start)?;
    let byte_end = destination_end + 1;
    let target = markdown_destination(&markdown[destination_start..destination_end])?;
    if target.is_empty() {
        return None;
    }

    let (normalized_target, anchor) = normalized_target_parts(&target);
    let resolved = known_targets.contains(&normalized_target);
    Some((
        WikiLink {
            kind: LinkKind::Markdown,
            target,
            normalized_target,
            anchor,
            alias: non_empty(markdown[label_start..label_end].trim()),
            byte_start,
            byte_end,
            resolved,
        },
        byte_end,
    ))
}

fn split_alias(value: &str, delimiter: char) -> (&str, Option<String>) {
    value
        .split_once(delimiter)
        .map_or((value, None), |(target, alias)| {
            (target, non_empty(alias.trim()))
        })
}

fn markdown_destination(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty() {
        return None;
    }

    if let Some(rest) = value.strip_prefix('<') {
        let end = rest.find('>')?;
        return non_empty(rest[..end].trim());
    }

    value
        .split_whitespace()
        .next()
        .and_then(|target| non_empty(target.trim()))
}

fn markdown_destination_end(markdown: &str, start: usize) -> Option<usize> {
    let mut escaped = false;
    let mut depth = 0usize;
    for (offset, ch) in markdown[start..].char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        match ch {
            '\\' => escaped = true,
            '(' => depth += 1,
            ')' if depth == 0 => return Some(start + offset),
            ')' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    None
}

fn markdown_label_end(markdown: &str, start: usize) -> Option<usize> {
    let mut depth = 0usize;
    for (offset, ch) in markdown[start..].char_indices() {
        let index = start + offset;
        if is_escaped(markdown, index) {
            continue;
        }
        match ch {
            '[' => depth += 1,
            ']' if depth == 0 => return Some(index),
            ']' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    None
}

fn wikilink_close_start(markdown: &str, start: usize) -> Option<usize> {
    let mut search_start = start;
    while let Some(offset) = markdown[search_start..].find("]]") {
        let close_start = search_start + offset;
        if !is_escaped(markdown, close_start) {
            return Some(close_start);
        }
        search_start = close_start + 1;
    }
    None
}

fn is_escaped(markdown: &str, byte_index: usize) -> bool {
    let bytes = markdown.as_bytes();
    let mut count = 0usize;
    let mut index = byte_index;
    while index > 0 && bytes[index - 1] == b'\\' {
        count += 1;
        index -= 1;
    }
    count % 2 == 1
}

fn markdown_code_ranges(markdown: &str) -> Vec<(usize, usize)> {
    let fenced = fenced_code_ranges(markdown);
    let mut ranges = fenced.clone();
    ranges.extend(inline_code_ranges(markdown, &fenced));
    ranges.sort_unstable();
    ranges
}

fn fenced_code_ranges(markdown: &str) -> Vec<(usize, usize)> {
    let mut ranges = Vec::new();
    let mut offset = 0usize;
    while offset < markdown.len() {
        let current_line_end = line_end(markdown, offset);
        if let Some((marker, marker_len)) = fence_marker(&markdown[offset..current_line_end]) {
            let start = offset;
            offset = current_line_end;
            while offset < markdown.len() {
                let candidate_end = line_end(markdown, offset);
                if fence_closes(&markdown[offset..candidate_end], marker, marker_len) {
                    offset = candidate_end;
                    break;
                }
                offset = candidate_end;
            }
            ranges.push((start, offset));
        } else {
            offset = current_line_end;
        }
    }
    ranges
}

fn inline_code_ranges(markdown: &str, excluded_ranges: &[(usize, usize)]) -> Vec<(usize, usize)> {
    let mut ranges = Vec::new();
    let mut offset = 0usize;
    while offset < markdown.len() {
        if let Some(next_offset) = code_range_end_containing(excluded_ranges, offset) {
            offset = next_offset;
            continue;
        }
        if markdown.as_bytes()[offset] == b'`' && !is_escaped(markdown, offset) {
            let tick_count = repeated_byte_count(markdown, offset, b'`');
            if let Some(close_start) =
                matching_backtick_run(markdown, offset + tick_count, tick_count)
            {
                let end = close_start + tick_count;
                ranges.push((offset, end));
                offset = end;
                continue;
            }
            offset += tick_count;
        } else {
            offset += next_char_len(markdown, offset);
        }
    }
    ranges
}

fn code_range_end_containing(ranges: &[(usize, usize)], offset: usize) -> Option<usize> {
    ranges
        .iter()
        .find_map(|(start, end)| (*start <= offset && offset < *end).then_some(*end))
}

fn line_end(markdown: &str, offset: usize) -> usize {
    markdown[offset..]
        .find('\n')
        .map_or(markdown.len(), |line_end| offset + line_end + 1)
}

fn fence_marker(line: &str) -> Option<(u8, usize)> {
    let bytes = line.as_bytes();
    let mut offset = 0usize;
    while matches!(bytes.get(offset), Some(b' ')) && offset < 3 {
        offset += 1;
    }
    let marker = *bytes.get(offset)?;
    if marker != b'`' && marker != b'~' {
        return None;
    }
    let marker_len = repeated_byte_count(line, offset, marker);
    (marker_len >= 3).then_some((marker, marker_len))
}

fn fence_closes(line: &str, marker: u8, marker_len: usize) -> bool {
    fence_marker(line).is_some_and(|(candidate, candidate_len)| {
        candidate == marker && candidate_len >= marker_len
    })
}

fn repeated_byte_count(markdown: &str, offset: usize, byte: u8) -> usize {
    markdown.as_bytes()[offset..]
        .iter()
        .take_while(|candidate| **candidate == byte)
        .count()
}

fn matching_backtick_run(markdown: &str, start: usize, tick_count: usize) -> Option<usize> {
    let mut offset = start;
    while offset < markdown.len() {
        if markdown.as_bytes()[offset] == b'`' {
            let run_count = repeated_byte_count(markdown, offset, b'`');
            if !is_escaped(markdown, offset) && run_count == tick_count {
                return Some(offset);
            }
            offset += run_count;
        } else {
            offset += next_char_len(markdown, offset);
        }
    }
    None
}

fn normalized_target_parts(target: &str) -> (String, Option<String>) {
    let target = target.trim();
    let (path, anchor) = target
        .split_once('#')
        .map_or((target, None), |(path, anchor)| {
            (path, non_empty(anchor.trim()))
        });

    let url_like = is_url_like_target(path);
    let mut normalized = path.trim().replace('\\', "/");
    if !url_like {
        while let Some(rest) = normalized.strip_prefix("./") {
            normalized = rest.to_string();
        }
        normalized = collapse_repeated_slashes(&normalized);
        normalized = normalized.trim_matches('/').to_string();

        let lower = normalized.to_ascii_lowercase();
        if lower.ends_with(".markdown") {
            normalized.truncate(normalized.len() - ".markdown".len());
        } else if lower.ends_with(".md") {
            normalized.truncate(normalized.len() - ".md".len());
        }
    }

    (normalized, anchor)
}

fn collapse_repeated_slashes(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    let mut previous_was_slash = false;
    for ch in value.chars() {
        if ch == '/' {
            if !previous_was_slash {
                output.push(ch);
            }
            previous_was_slash = true;
        } else {
            output.push(ch);
            previous_was_slash = false;
        }
    }
    output
}

fn is_url_like_target(target: &str) -> bool {
    // Add extra external schemes here when gwiki should preserve them exactly
    // instead of normalizing them as vault-relative paths.
    target.contains("://") || target.starts_with("//") || target.starts_with("\\\\")
}

fn non_empty(value: &str) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

fn is_image_marker(markdown: &str, offset: usize) -> bool {
    offset > 0 && markdown.as_bytes()[offset - 1] == b'!'
}

fn next_char_len(markdown: &str, offset: usize) -> usize {
    markdown[offset..].chars().next().map_or(1, char::len_utf8)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_all_link_shapes() {
        let markdown = concat!(
            "See [[knowledge/concepts/Existing Note|existing note]], ",
            "[guide](docs/Guide.md), ",
            "[[Missing Note]], and ",
            "[gone](missing/page.md).\n",
        );
        let links = extract_links(markdown, ["knowledge/concepts/Existing Note", "docs/Guide"]);

        assert_eq!(links.len(), 4);
        assert_eq!(links[0].kind, LinkKind::Wikilink);
        assert_eq!(links[0].target, "knowledge/concepts/Existing Note");
        assert_eq!(
            links[0].normalized_target,
            "knowledge/concepts/Existing Note"
        );
        assert_eq!(links[0].alias.as_deref(), Some("existing note"));
        assert!(links[0].resolved);
        assert_eq!(
            &markdown[links[0].byte_start..links[0].byte_end],
            "[[knowledge/concepts/Existing Note|existing note]]"
        );

        assert_eq!(links[1].kind, LinkKind::Markdown);
        assert_eq!(links[1].target, "docs/Guide.md");
        assert_eq!(links[1].normalized_target, "docs/Guide");
        assert_eq!(links[1].alias.as_deref(), Some("guide"));
        assert!(links[1].resolved);

        assert_eq!(links[2].kind, LinkKind::Wikilink);
        assert_eq!(links[2].target, "Missing Note");
        assert_eq!(links[2].normalized_target, "Missing Note");
        assert!(!links[2].resolved);

        assert_eq!(links[3].kind, LinkKind::Markdown);
        assert_eq!(links[3].target, "missing/page.md");
        assert_eq!(links[3].normalized_target, "missing/page");
        assert_eq!(links[3].alias.as_deref(), Some("gone"));
        assert!(!links[3].resolved);
    }

    #[test]
    fn url_like_targets_are_not_normalized_as_vault_paths() {
        assert_eq!(
            normalize_wiki_path("//cdn.example.test/docs/page.md"),
            "//cdn.example.test/docs/page.md"
        );
        assert_eq!(
            normalize_wiki_path(r"\\server\share\Page.md"),
            "//server/share/Page.md"
        );
    }

    #[test]
    fn markdown_links_accept_balanced_parentheses_in_destinations() {
        let links = extract_links("[Spec](docs/Parser_(v2).md)", ["docs/Parser_(v2).md"]);
        assert_eq!(links[0].target, "docs/Parser_(v2).md");
        assert_eq!(links[0].normalized_target, "docs/Parser_(v2)");
        assert!(links[0].resolved);
    }

    #[test]
    fn markdown_link_labels_ignore_escaped_brackets() {
        let links = extract_links(r"[Escaped \] label](docs/Guide.md)", ["docs/Guide"]);

        assert_eq!(links.len(), 1);
        assert_eq!(links[0].target, "docs/Guide.md");
        assert_eq!(links[0].alias.as_deref(), Some(r"Escaped \] label"));
    }

    #[test]
    fn markdown_link_labels_accept_nested_brackets() {
        let links = extract_links("[See [note]](docs/Guide.md)", ["docs/Guide"]);

        assert_eq!(links.len(), 1);
        assert_eq!(links[0].target, "docs/Guide.md");
        assert_eq!(links[0].alias.as_deref(), Some("See [note]"));
    }

    #[test]
    fn wikilinks_ignore_escaped_closing_brackets() {
        let markdown = r"[[Topic \]] still inside]] and [[Done]]";
        let links = extract_links(markdown, ["Done"]);

        assert_eq!(links.len(), 2);
        assert_eq!(links[0].target, r"Topic \]] still inside");
        assert_eq!(links[1].target, "Done");
    }

    #[test]
    fn links_ignore_code_spans_and_fences() {
        let markdown = concat!(
            "Use `[[code/files/{file}|{file}]]` as a template.\n",
            "```md\n",
            "[[Missing in fence]]\n",
            "[missing](inside-fence.md)\n",
            "```\n",
            "Then see [[Real]] and [Guide](guide.md).\n",
        );
        let links = extract_links(markdown, ["Real", "guide"]);

        assert_eq!(links.len(), 2);
        assert_eq!(links[0].target, "Real");
        assert_eq!(links[1].target, "guide.md");
    }

    #[test]
    fn inline_code_inside_fenced_blocks_is_excluded() {
        let markdown = concat!(
            "```md\n",
            "`[[Inside inline code in fence]]`\n",
            "```\n",
            "[[Outside]]\n",
        );
        let links = extract_links(markdown, ["Outside"]);

        assert_eq!(links.len(), 1);
        assert_eq!(links[0].target, "Outside");
    }
}
